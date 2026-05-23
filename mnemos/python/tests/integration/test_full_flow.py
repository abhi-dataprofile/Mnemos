"""End-to-end orchestrator flow against a synthetic PR.

This is the in-process cousin of the ``docker compose`` acceptance run
in ``mnemos-plan/08-phase-7-integration-polish.md`` § 1. The sandboxed
test harness doesn't have Docker or a real GitHub App to work with, so
we instead drive every Python-side component end to end:

    AgentContext -> Coordinator(3 agents) -> build_review_payload -> wire-shape

The agents run for real (they're already pure Python except for LLM +
graph calls, which we stub with duck-typed fakes). The coordinator
runs them concurrently with its actual timeout logic. The formatter
emits the same dict the TS callback receives in production, so any
payload-shape drift between agents and the callback surfaces here.

What this test does NOT cover:

- Real webhook delivery from GitHub
- Real Postgres / pgvector queries (those live in Phase 7's deferred
  real-DB acceptance run)
- RQ queue + worker process boundary
- TS callback HMAC signing
- A live Anthropic call

Those require the laptop acceptance run.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from codereview.agents.base import (
    AgentContext,
    ChangedFile,
    ChangedSymbol,
    PullRequestSnapshot,
)
from codereview.agents.conflict.detector import ConflictDetector
from codereview.agents.context.packager import ContextPackager
from codereview.agents.router.router import ReviewerRouter
from codereview.orchestration.coordinator import Coordinator
from codereview.orchestration.formatter import build_review_payload

# -- Test doubles ----------------------------------------------------------


@dataclass(frozen=True)
class _PersonRef:
    id: UUID
    github_login: str


@dataclass(frozen=True)
class _CountRow:
    person: _PersonRef
    count: int


@dataclass
class _StubGraph:
    """In-memory graph surface for the integration harness.

    Implements just enough of :class:`GraphClient` for the three agents
    to produce meaningful output. Everything else degrades to neutral
    defaults via the duck-typed ``getattr`` pattern the agents already
    use.
    """

    file_ids: dict[str, UUID] = field(default_factory=dict)
    authors_by_file: dict[UUID, list[_CountRow]] = field(default_factory=dict)
    reviewers_by_file: dict[UUID, list[_CountRow]] = field(default_factory=dict)
    last_activity: dict[UUID, Any] = field(default_factory=dict)
    open_loads: dict[UUID, int] = field(default_factory=dict)

    async def file_by_path(self, _repo_id: UUID, path: str) -> UUID | None:
        return self.file_ids.get(path)

    async def authors_of_file(
        self, file_id: UUID, _since: Any
    ) -> list[_CountRow]:
        return self.authors_by_file.get(file_id, [])

    async def reviewers_of_file(
        self, file_id: UUID, _since: Any
    ) -> list[_CountRow]:
        return self.reviewers_by_file.get(file_id, [])

    async def last_activity_at(self, person_id: UUID) -> Any:
        return self.last_activity.get(person_id)

    async def open_prs_assigned_to(self, person_id: UUID) -> int:
        return self.open_loads.get(person_id, 0)

    async def call_graph_overlap_counts(
        self, _file_ids: list[UUID], _since: Any
    ) -> dict[UUID, int]:
        return {}

    async def review_acceptance_rate(
        self, _person_id: UUID, _since: Any
    ) -> tuple[int, int]:
        return (0, 0)

    # Context Packager surfaces — each returns something tiny and real,
    # so the packet has some content. None of them are required; the
    # packager degrades gracefully when they're absent.
    async def similar_prs_scored(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    async def files_touched_by_pr(self, *_a: Any, **_k: Any) -> list[str]:
        return []


class _StubLLM:
    """Minimal LLM surface. The three agents tolerate a missing
    ``structured_call`` — ContextPackager falls back to a deterministic
    one-liner and ConflictDetector has other heuristic paths."""

    async def structured_call(self, *_a: Any, **_k: Any) -> None:
        raise RuntimeError("stub LLM: no structured_call")


# -- Helpers --------------------------------------------------------------


def _pr() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=101,
        title="Rename generate_pdf; bump refund flow",
        body="Fixes #42.\n\nRelated to ADR-002.",
        author="abhi",
        head_sha="h" * 40,
        base_sha="b" * 40,
        changed_files=[
            ChangedFile(path="src/billing/invoice.py", change_kind="modified"),
            ChangedFile(path="src/billing/refunds.py", change_kind="modified"),
        ],
        changed_symbols=[
            ChangedSymbol(
                qualified_name="billing.invoice.generate_pdf",
                kind="function",
                change_kind="renamed",
                old_signature="generate_pdf(invoice_id: int) -> bytes",
                new_signature="generate_pdf(invoice: Invoice) -> bytes",
                file_path="src/billing/invoice.py",
            ),
        ],
    )


def _ctx() -> AgentContext:
    invoice_id = uuid4()
    refunds_id = uuid4()
    alice = uuid4()
    bob = uuid4()

    graph = _StubGraph(
        file_ids={
            "src/billing/invoice.py": invoice_id,
            "src/billing/refunds.py": refunds_id,
        },
        authors_by_file={
            invoice_id: [
                _CountRow(_PersonRef(alice, "alice"), 8),
                _CountRow(_PersonRef(bob, "bob"), 2),
            ],
            refunds_id: [_CountRow(_PersonRef(alice, "alice"), 5)],
        },
        reviewers_by_file={
            invoice_id: [_CountRow(_PersonRef(bob, "bob"), 4)],
        },
        last_activity={alice: _recent(), bob: _recent()},
        open_loads={alice: 1, bob: 2},
    )

    return AgentContext(
        pr=_pr(),
        repo_id=uuid4(),
        graph=graph,
        llm=_StubLLM(),
        config={},
    )


def _recent() -> Any:
    import datetime as dt

    return dt.datetime(2026, 4, 20, tzinfo=dt.timezone.utc)


# -- The test ------------------------------------------------------------


async def test_full_flow_produces_complete_payload() -> None:
    """Coordinator + three real agents + formatter → full wire payload.

    Asserts that:

    * A payload is produced (no exception reaches the caller).
    * All three sections' data channels are populated OR explicitly
      empty (not silently missing).
    * ``mnemos_version`` is stamped.
    * The wall-clock is well under the 90-second review SLO.
    * No agent crashed (``failed_agents`` absent).
    """

    coordinator = Coordinator(
        agents=[ConflictDetector(), ContextPackager(), ReviewerRouter()],
        per_agent_timeout_s=10.0,
    )

    ctx = _ctx()
    started = time.monotonic()
    coordinator_result = await coordinator.run(ctx)
    wall_time = time.monotonic() - started

    payload = build_review_payload(coordinator_result=coordinator_result)

    # Sanity: no agent crashed. Any crash means a regression in one of
    # the three agents' degradation paths (graph missing, LLM missing).
    assert "failed_agents" not in payload, payload.get("failed_agents")

    # Summary is always present (either passed in or synthesised).
    assert isinstance(payload["summary"], str)
    assert payload["summary"].strip() != ""

    # Conflicts list is always a list (may be empty if no conflicts
    # were detected against our synthetic graph).
    assert isinstance(payload["conflicts"], list)

    # Version stamp reaches the wire.
    assert "mnemos_version" in payload
    assert isinstance(payload["mnemos_version"], str)

    # Wall-clock is well inside the 90-second SLO — we have no real
    # network here, so sub-second is expected. Failing this catches a
    # regression like someone accidentally waiting on a real LLM.
    assert wall_time < 10.0, f"coordinator took {wall_time:.1f}s"


async def test_full_flow_surfaces_failed_agents_without_crashing() -> None:
    """A single agent exception becomes metadata, not a failed review."""

    class _BrokenAgent(ConflictDetector):
        async def run(self, _ctx: AgentContext) -> Any:  # type: ignore[override]
            raise RuntimeError("simulated agent crash")

    coordinator = Coordinator(
        agents=[_BrokenAgent(), ContextPackager(), ReviewerRouter()],
        per_agent_timeout_s=5.0,
    )
    result = await coordinator.run(_ctx())
    payload = build_review_payload(coordinator_result=result)

    assert "failed_agents" in payload
    assert "conflict_detector" in payload["failed_agents"]
    # But the other two agents' output still got through.
    assert isinstance(payload["conflicts"], list)


async def test_full_flow_respects_per_agent_timeout() -> None:
    """A hung agent is timed out, not left to stall the whole review."""

    class _HungAgent(ConflictDetector):
        async def run(self, _ctx: AgentContext) -> Any:  # type: ignore[override]
            await asyncio.sleep(10.0)

    coordinator = Coordinator(
        agents=[_HungAgent(), ContextPackager(), ReviewerRouter()],
        per_agent_timeout_s=0.2,
    )
    started = time.monotonic()
    result = await coordinator.run(_ctx())
    wall_time = time.monotonic() - started

    payload = build_review_payload(coordinator_result=result)
    assert "failed_agents" in payload
    assert "conflict_detector" in payload["failed_agents"]
    # Coordinator capped us at ~0.2s for the hung one; the other two
    # finish quickly. 2s is a comfortably high ceiling for CI flake.
    assert wall_time < 2.0, f"coordinator took {wall_time:.1f}s despite timeout"


# pytest-asyncio: these tests are auto-picked up via asyncio_mode=auto
# in pyproject.toml. No decorator needed.
_ = pytest  # keep the import active for future test additions
