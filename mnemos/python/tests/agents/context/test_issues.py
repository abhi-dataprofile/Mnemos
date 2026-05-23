"""Unit tests for :mod:`codereview.agents.context.issues`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from codereview.agents.context.issues import find_linked_issues, parse_pr_body

# -- parse_pr_body (pure) -------------------------------------------------


def test_parses_empty_body_as_empty() -> None:
    assert parse_pr_body("") == []
    assert parse_pr_body("   \n") == []


def test_parses_fixes_issue_reference() -> None:
    items = parse_pr_body("Fixes #123")
    assert len(items) == 1
    assert items[0].kind == "github"
    assert items[0].identifier == "#123"
    assert items[0].number == 123


def test_parses_variant_github_verbs() -> None:
    """Recognises ``fix(es|ed)?``, ``clos(es|ed)?``, ``resolv(es|ed)?``."""

    body = "fix #1, fixes #2, fixed #3, closes #4, closed #5, resolves #6, resolved #7"
    items = parse_pr_body(body)
    assert sorted(i.number for i in items if i.number is not None) == [1, 2, 3, 4, 5, 6, 7]


def test_verb_is_case_insensitive() -> None:
    items = parse_pr_body("FIXES #10\nCloses #11")
    numbers = sorted(i.number for i in items if i.number is not None)
    assert numbers == [10, 11]


def test_dedupes_repeated_github_refs() -> None:
    items = parse_pr_body("fixes #1 and also closes #1")
    assert len(items) == 1
    assert items[0].number == 1


def test_parses_explicit_external_verb_ref() -> None:
    items = parse_pr_body("Fixes ACME-42")
    assert [(i.kind, i.identifier) for i in items] == [("external", "ACME-42")]


def test_parses_bare_external_id() -> None:
    items = parse_pr_body("See ACME-99 for background.")
    assert [(i.kind, i.identifier) for i in items] == [("external", "ACME-99")]


def test_external_id_case_sensitive_on_prefix() -> None:
    """Lowercase prefixes must not match — otherwise ordinary words collide."""
    assert parse_pr_body("see acme-42") == []


def test_ignores_free_floating_numbers() -> None:
    """Random numeric tokens should not generate issue links."""
    assert parse_pr_body("bumped to 1234 and noticed 5678") == []


def test_preserves_mixed_github_and_external() -> None:
    items = parse_pr_body("Fixes #5\nRelated: ACME-22")
    kinds = [(i.kind, i.identifier) for i in items]
    assert kinds == [("github", "#5"), ("external", "ACME-22")]


def test_external_dedupes_across_verb_and_bare() -> None:
    """`fixes ACME-42` then `ACME-42` again should produce one entry."""
    items = parse_pr_body("Fixes ACME-42 — see also ACME-42 below.")
    external = [i for i in items if i.kind == "external"]
    assert len(external) == 1


# -- find_linked_issues (graph-enriched) ----------------------------------


@dataclass
class _FakeGraph:
    issues: dict[tuple[UUID, int], tuple[UUID, str, str]] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)

    async def issue_by_number(
        self, repo_id: UUID, number: int
    ) -> tuple[UUID, str, str] | None:
        self.calls.append(number)
        return self.issues.get((repo_id, number))


async def test_empty_body_short_circuits_graph() -> None:
    graph = _FakeGraph()
    out = await find_linked_issues(repo_id=uuid4(), pr_body="", graph=graph)
    assert out == []
    assert graph.calls == []


async def test_enriches_github_issue_with_title_and_state() -> None:
    repo_id = uuid4()
    graph = _FakeGraph(
        issues={(repo_id, 10): (uuid4(), "auth bug", "open")}
    )
    out = await find_linked_issues(
        repo_id=repo_id, pr_body="fixes #10", graph=graph
    )
    assert len(out) == 1
    assert out[0].kind == "github"
    assert out[0].number == 10
    assert out[0].title == "auth bug"
    assert out[0].state == "open"


async def test_unknown_github_issue_falls_back_to_bare_reference() -> None:
    repo_id = uuid4()
    graph = _FakeGraph(issues={})
    out = await find_linked_issues(
        repo_id=repo_id, pr_body="fixes #99", graph=graph
    )
    assert len(out) == 1
    assert out[0].kind == "github"
    assert out[0].number == 99
    assert out[0].title is None
    assert out[0].state is None


async def test_external_issues_not_enriched() -> None:
    repo_id = uuid4()
    graph = _FakeGraph()
    out = await find_linked_issues(
        repo_id=repo_id, pr_body="Fixes ACME-7", graph=graph
    )
    assert len(out) == 1
    assert out[0].kind == "external"
    # issue_by_number should never be called for external IDs.
    assert graph.calls == []


async def test_graph_missing_issue_lookup_degrades_cleanly() -> None:
    class _Bare:
        pass

    out = await find_linked_issues(
        repo_id=uuid4(), pr_body="fixes #1", graph=_Bare()
    )
    # With no issue_by_number we keep the bare github reference.
    assert [(i.kind, i.number) for i in out] == [("github", 1)]


async def test_issue_lookup_error_is_swallowed() -> None:
    class _RaisingGraph:
        async def issue_by_number(self, _repo_id: UUID, _number: int) -> Any:
            raise RuntimeError("db down")

    out = await find_linked_issues(
        repo_id=uuid4(), pr_body="fixes #5", graph=_RaisingGraph()
    )
    # Fall back to unenriched github reference rather than crashing.
    assert [(i.kind, i.number, i.title) for i in out] == [("github", 5, None)]
