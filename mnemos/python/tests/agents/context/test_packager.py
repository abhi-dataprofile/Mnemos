"""Unit tests for :mod:`codereview.agents.context.packager`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from codereview.agents.base import (
    AgentContext,
    ChangedFile,
    PullRequestSnapshot,
)
from codereview.agents.context import ContextPackager
from codereview.agents.context.prompts import ContextSummary

# -- Fakes ----------------------------------------------------------------


@dataclass
class _PRRef:
    id: UUID
    number: int
    title: str
    body: str = ""
    author_login: str | None = None
    merged_at: Any = None


@dataclass
class _ADR:
    id: UUID
    title: str
    status: str
    body: str = ""


@dataclass
class _Commit:
    sha: str
    message: str = ""
    author_login: str | None = None


@dataclass
class _FakeGraph:
    """Full-surface fake covering every method the packager might call."""

    similar_prs_scored_pool: list[tuple[_PRRef, float]] = field(default_factory=list)
    files_by_pr: dict[UUID, list[str]] = field(default_factory=dict)
    similar_adrs_pool: list[_ADR] = field(default_factory=list)
    file_ids: dict[str, UUID] = field(default_factory=dict)
    commits_by_file: dict[UUID, list[_Commit]] = field(default_factory=dict)
    issues: dict[tuple[UUID, int], tuple[UUID, str, str]] = field(default_factory=dict)
    reverts: dict[UUID, list[_Commit]] = field(default_factory=dict)
    churn: dict[UUID, int] = field(default_factory=dict)

    async def similar_prs_scored(
        self, _embedding: list[float], k: int = 5
    ) -> list[tuple[_PRRef, float]]:
        return self.similar_prs_scored_pool[:k]

    async def files_touched_by_pr(self, pr_id: UUID) -> list[str]:
        return self.files_by_pr.get(pr_id, [])

    async def similar_adrs(self, _embedding: list[float], k: int = 5) -> list[_ADR]:
        return self.similar_adrs_pool[:k]

    async def file_by_path(self, _repo_id: UUID, path: str) -> UUID | None:
        return self.file_ids.get(path)

    async def recent_commits_touching(
        self, file_id: UUID, limit: int = 5
    ) -> list[_Commit]:
        return self.commits_by_file.get(file_id, [])[:limit]

    async def issue_by_number(
        self, repo_id: UUID, number: int
    ) -> tuple[UUID, str, str] | None:
        return self.issues.get((repo_id, number))

    async def revert_commits_touching(
        self, file_id: UUID, _since: Any
    ) -> list[_Commit]:
        return self.reverts.get(file_id, [])

    async def commit_count_for_file_since(self, file_id: UUID, _since: Any) -> int:
        return self.churn.get(file_id, 0)


class _FakeLLM:
    """LLM whose embedder returns a fixed vector and whose structured_call returns canned text."""

    def __init__(
        self,
        *,
        embed_vector: list[float] | None = None,
        summary_text: str = "short narrative",
        embed_raises: bool = False,
        structured_raises: bool = False,
    ) -> None:
        self._embed_vector = embed_vector if embed_vector is not None else [0.1, 0.2, 0.3]
        self._summary_text = summary_text
        self._embed_raises = embed_raises
        self._structured_raises = structured_raises
        self.embed_calls: list[str] = []

    async def embed_prose(self, text: str) -> list[float]:
        if self._embed_raises:
            raise RuntimeError("embedder down")
        self.embed_calls.append(text)
        return list(self._embed_vector)

    async def structured_call(self, **_kwargs: Any) -> ContextSummary:
        if self._structured_raises:
            raise RuntimeError("anthropic 500")
        return ContextSummary(summary=self._summary_text)


# -- Helpers --------------------------------------------------------------


def _pr(
    *,
    number: int = 99,
    title: str = "Tighten retry policy",
    body: str = "Fixes #3",
    author: str = "abhi",
    files: list[str] | None = None,
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=number,
        title=title,
        body=body,
        author=author,
        head_sha="h" * 40,
        base_sha="b" * 40,
        changed_files=[
            ChangedFile(path=p, change_kind="modified", patch="+x\n-y\n")
            for p in (files or ["retry.py"])
        ],
    )


def _ctx(
    *, pr: PullRequestSnapshot, graph: Any, llm: Any, repo_id: UUID | None = None
) -> AgentContext:
    return AgentContext(
        pr=pr,
        repo_id=repo_id or uuid4(),
        graph=graph,
        llm=llm,
    )


# -- Happy path -----------------------------------------------------------


async def test_packager_metadata_includes_context_packet() -> None:
    graph = _FakeGraph()
    llm = _FakeLLM()
    ctx = _ctx(pr=_pr(), graph=graph, llm=llm)

    out = await ContextPackager().run(ctx)

    assert out.agent_name == "context_packager"
    assert out.findings == []
    assert "context_packet" in out.metadata
    assert isinstance(out.metadata["context_packet"], dict)


async def test_packager_records_section_meta() -> None:
    ctx = _ctx(pr=_pr(), graph=_FakeGraph(), llm=_FakeLLM())
    out = await ContextPackager().run(ctx)

    sections = out.metadata["sections"]
    for key in (
        "related_prs",
        "related_adrs",
        "recent_commits",
        "linked_issues",
        "risk_notes",
        "narrative",
    ):
        assert key in sections


async def test_packet_assembles_related_prs_and_history() -> None:
    prior = _PRRef(id=uuid4(), number=12, title="prior tweak", author_login="bob")
    adr = _ADR(id=uuid4(), title="ADR-007 idempotency", status="accepted")
    fid = uuid4()
    repo_id = uuid4()

    graph = _FakeGraph(
        similar_prs_scored_pool=[(prior, 0.9)],
        files_by_pr={prior.id: ["retry.py"]},
        similar_adrs_pool=[adr],
        file_ids={"retry.py": fid},
        commits_by_file={fid: [_Commit(sha="aaabbbcccddd", message="bump backoff")]},
        issues={(repo_id, 3): (uuid4(), "flaky retries", "open")},
    )
    ctx = _ctx(pr=_pr(body="Fixes #3"), graph=graph, llm=_FakeLLM(), repo_id=repo_id)

    out = await ContextPackager().run(ctx)
    packet = out.metadata["context_packet"]

    assert packet["related_prs"][0]["number"] == 12
    assert "score" not in packet["related_prs"][0]  # internal field stripped
    assert packet["related_adrs"][0]["title"] == "ADR-007 idempotency"
    assert packet["recent_commits"][0]["sha"] == "aaabbbcccddd"
    assert packet["linked_issues"][0]["number"] == 3
    assert packet["narrative"] == "short narrative"


async def test_packet_carries_large_pr_risk_note() -> None:
    patch = "\n".join(["+x"] * 600 + ["-y"] * 20)
    pr = PullRequestSnapshot(
        number=99,
        title="big change",
        body="",
        author="abhi",
        head_sha="h" * 40,
        base_sha="b" * 40,
        changed_files=[ChangedFile(path="x.py", change_kind="modified", patch=patch)],
    )
    ctx = _ctx(pr=pr, graph=_FakeGraph(), llm=_FakeLLM())

    out = await ContextPackager().run(ctx)
    risks = out.metadata["context_packet"].get("risk_notes", [])
    assert any("Large PR" in n for n in risks)


# -- Degradation / isolation ---------------------------------------------


async def test_llm_without_embed_prose_skips_similarity_sections() -> None:
    class _LLMNoEmbed:
        async def structured_call(self, **_kwargs: Any) -> ContextSummary:
            return ContextSummary(summary="narr")

    graph = _FakeGraph(
        similar_prs_scored_pool=[
            (_PRRef(id=uuid4(), number=5, title="ignored"), 0.9)
        ]
    )
    ctx = _ctx(pr=_pr(), graph=graph, llm=_LLMNoEmbed())

    out = await ContextPackager().run(ctx)
    packet = out.metadata["context_packet"]
    # No embedding ⇒ related_prs and related_adrs degrade to empty,
    # so those keys are absent from to_wire().
    assert "related_prs" not in packet
    assert "related_adrs" not in packet
    assert out.metadata.get("embedding_skipped_reason")


async def test_embed_failure_does_not_crash_run() -> None:
    ctx = _ctx(pr=_pr(), graph=_FakeGraph(), llm=_FakeLLM(embed_raises=True))
    out = await ContextPackager().run(ctx)
    assert "embed_prose failed" in out.metadata["embedding_skipped_reason"]


async def test_llm_summary_failure_falls_back_to_deterministic_string() -> None:
    prior = _PRRef(id=uuid4(), number=12, title="prior")
    graph = _FakeGraph(
        similar_prs_scored_pool=[(prior, 0.9)],
        files_by_pr={prior.id: ["retry.py"]},
    )
    ctx = _ctx(pr=_pr(), graph=graph, llm=_FakeLLM(structured_raises=True))

    out = await ContextPackager().run(ctx)
    narrative = out.metadata["context_packet"]["narrative"]
    assert "Packet assembled" in narrative


async def test_one_section_error_does_not_poison_the_packet() -> None:
    """A graph whose ``similar_prs_scored`` explodes must not sink the rest."""

    class _PartiallyBroken(_FakeGraph):
        async def similar_prs_scored(
            self, _embedding: list[float], k: int = 5
        ) -> list[tuple[_PRRef, float]]:
            raise RuntimeError("pgvector exploded")

    graph = _PartiallyBroken(
        similar_adrs_pool=[_ADR(id=uuid4(), title="ADR-X", status="accepted")]
    )
    ctx = _ctx(pr=_pr(), graph=graph, llm=_FakeLLM())

    out = await ContextPackager().run(ctx)
    packet = out.metadata["context_packet"]
    # Related PRs section absent (error swallowed inside find_related_prs),
    # but ADRs still made it through.
    assert "related_prs" not in packet
    assert packet["related_adrs"][0]["title"] == "ADR-X"
    # Section metadata records zero related PRs — the error is swallowed
    # one layer down, so the packager only sees an empty list.
    assert out.metadata["sections"]["related_prs"]["count"] == 0


async def test_empty_packet_narrative_is_empty_string() -> None:
    """When there is nothing to summarise, narrative stays empty."""

    class _LLMNoEmbed:
        async def structured_call(self, **_kwargs: Any) -> ContextSummary:  # pragma: no cover
            raise AssertionError("summariser should not be called for empty packet")

    ctx = _ctx(pr=_pr(body=""), graph=_FakeGraph(), llm=_LLMNoEmbed())
    out = await ContextPackager().run(ctx)
    packet = out.metadata["context_packet"]
    assert packet == {}  # to_wire() omits empties


async def test_pr_itself_excluded_from_related() -> None:
    """Even if graph returns the current PR, it must not surface."""

    self_pr = _PRRef(id=uuid4(), number=99, title="self")
    other = _PRRef(id=uuid4(), number=12, title="other", author_login="bob")
    graph = _FakeGraph(
        similar_prs_scored_pool=[(self_pr, 0.99), (other, 0.9)],
        files_by_pr={self_pr.id: ["retry.py"], other.id: ["retry.py"]},
    )
    ctx = _ctx(pr=_pr(number=99), graph=graph, llm=_FakeLLM())

    out = await ContextPackager().run(ctx)
    numbers = [p["number"] for p in out.metadata["context_packet"].get("related_prs", [])]
    assert 99 not in numbers
    assert 12 in numbers
