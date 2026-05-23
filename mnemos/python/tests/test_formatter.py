"""Tests for the review output aggregator.

The formatter sits on the seam between Python agents and the TypeScript
callback, so these tests lock the wire shape. Cross-language contract
tests (see ``tests/test_contract.py``) cover the request direction; this
module covers the response direction.
"""

from __future__ import annotations

from codereview.agents.base import AgentResult, Finding, Location
from codereview.orchestration.coordinator import (
    AgentRunOutcome,
    CoordinatorResult,
)
from codereview.orchestration.formatter import build_review_payload, summarize

# -- Helpers ----------------------------------------------------------------


def _finding(
    severity: str,
    kind: str = "semantic",
    title: str = "t",
    detail: str = "d",
    path: str | None = None,
    line: int | None = None,
    related: list[str] | None = None,
    action: str | None = None,
) -> Finding:
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        kind=kind,
        title=title,
        detail=detail,
        locations=[Location(path=path, line=line)] if path else [],
        related_symbols=related or [],
        suggested_action=action,
    )


def _result(
    *findings: Finding, agent_name: str = "conflict_detector"
) -> AgentResult:
    return AgentResult(agent_name=agent_name, findings=list(findings))


def _coord(
    outcomes: list[AgentRunOutcome] | None = None,
    *,
    results: list[AgentResult] | None = None,
) -> CoordinatorResult:
    if outcomes is not None:
        return CoordinatorResult(outcomes=outcomes)
    wrapped = [
        AgentRunOutcome(agent_name=r.agent_name, result=r)
        for r in (results or [])
    ]
    return CoordinatorResult(outcomes=wrapped)


# -- Shape ------------------------------------------------------------------


def test_empty_result_returns_reassuring_summary() -> None:
    payload = build_review_payload(coordinator_result=_coord(results=[]))
    assert payload["summary"].startswith("Mnemos ran")
    assert "nothing to flag" in payload["summary"]
    assert payload["conflicts"] == []
    # Optional sections absent when empty.
    assert "context" not in payload
    assert "suggested_reviewers" not in payload
    assert "failed_agents" not in payload


def test_single_finding_mapped_to_conflict_shape() -> None:
    f = _finding(
        "warning",
        kind="convention",
        title="ADR-002 drift",
        detail="tuple return",
        path="src/billing/refunds.py",
        related=["issue_refund"],
        action="Raise BillingError instead.",
    )
    payload = build_review_payload(coordinator_result=_coord(results=[_result(f)]))
    conflicts = payload["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c == {
        "severity": "warning",
        "kind": "convention",
        "title": "ADR-002 drift",
        "detail": "tuple return",
        "locations": [{"path": "src/billing/refunds.py"}],
        "related_symbols": ["issue_refund"],
        "suggested_action": "Raise BillingError instead.",
    }


def test_location_line_included_when_present() -> None:
    f = _finding("info", path="x.py", line=42)
    payload = build_review_payload(coordinator_result=_coord(results=[_result(f)]))
    loc = payload["conflicts"][0]["locations"][0]
    assert loc == {"path": "x.py", "line": 42}


def test_location_line_omitted_when_none() -> None:
    f = _finding("info", path="x.py", line=None)
    loc = build_review_payload(coordinator_result=_coord(results=[_result(f)]))[
        "conflicts"
    ][0]["locations"][0]
    assert loc == {"path": "x.py"}


# -- Sort order -------------------------------------------------------------


def test_conflicts_sorted_blocking_warning_info() -> None:
    findings = [
        _finding("info", title="low"),
        _finding("blocking", title="high"),
        _finding("warning", title="mid"),
    ]
    payload = build_review_payload(
        coordinator_result=_coord(results=[_result(*findings)])
    )
    titles = [c["title"] for c in payload["conflicts"]]
    assert titles == ["high", "mid", "low"]


# -- Dedup across agents ----------------------------------------------------


def test_duplicate_across_agents_collapsed() -> None:
    """Two agents surfacing the same issue → one entry in the payload."""

    f = _finding("warning", title="ADR-002 drift", path="src/billing/refunds.py")
    payload = build_review_payload(
        coordinator_result=_coord(
            results=[
                _result(f, agent_name="conflict_detector"),
                _result(f, agent_name="hypothetical_other"),
            ]
        )
    )
    assert len(payload["conflicts"]) == 1


# -- Failed agents ---------------------------------------------------------


def test_failed_agents_surfaced() -> None:
    ok_outcome = AgentRunOutcome(
        agent_name="conflict_detector",
        result=_result(_finding("info", title="all good")),
    )
    crashed = AgentRunOutcome(
        agent_name="some_other_agent",
        result=None,
        error="RuntimeError('boom')",
    )
    timed_out = AgentRunOutcome(
        agent_name="slow_agent",
        result=None,
        timed_out=True,
    )
    payload = build_review_payload(
        coordinator_result=_coord(outcomes=[ok_outcome, crashed, timed_out])
    )
    assert payload["failed_agents"] == ["some_other_agent", "slow_agent"]
    # Successful agent's findings still made it through.
    assert len(payload["conflicts"]) == 1


# -- Summary generation ----------------------------------------------------


def test_summary_counts_by_severity() -> None:
    findings = [
        _finding("blocking", title="a"),
        _finding("blocking", title="b"),
        _finding("warning", title="c"),
        _finding("info", title="d"),
    ]
    s = summarize(findings)
    assert "2 blocking" in s
    assert "1 warning" in s
    assert "1 info" in s


def test_summary_explicit_override_preserved() -> None:
    findings = [_finding("blocking", title="x")]
    payload = build_review_payload(
        coordinator_result=_coord(results=[_result(*findings)]),
        summary="Custom summary",
    )
    assert payload["summary"] == "Custom summary"


# -- Optional input plumbing ----------------------------------------------


def test_context_and_reviewers_pass_through() -> None:
    ctx = {"narrative": "related PR #7"}
    reviewers = [{"login": "abhi", "score": 0.9, "rationale": "codeowner"}]
    payload = build_review_payload(
        coordinator_result=_coord(results=[]),
        context=ctx,
        suggested_reviewers=reviewers,
    )
    assert payload["context"] == ctx
    assert payload["suggested_reviewers"] == reviewers


def test_optional_fields_absent_when_not_provided() -> None:
    payload = build_review_payload(coordinator_result=_coord(results=[]))
    assert "context" not in payload
    assert "suggested_reviewers" not in payload


# -- Context routing from agent metadata ----------------------------------


def _result_with_packet(packet: dict, agent_name: str = "context_packager") -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        findings=[],
        metadata={"context_packet": packet},
    )


def test_context_packet_in_metadata_promoted_to_top_level() -> None:
    """Packager emits via ``metadata['context_packet']`` — formatter routes it."""

    packet = {"narrative": "30-second briefing", "related_prs": [{"number": 7, "title": "x"}]}
    payload = build_review_payload(
        coordinator_result=_coord(results=[_result_with_packet(packet)])
    )
    assert payload["context"] == packet


def test_explicit_context_override_wins_over_metadata() -> None:
    """Caller-supplied context kwarg takes precedence over agent metadata."""

    metadata_packet = {"narrative": "from agent"}
    explicit = {"narrative": "explicit override"}
    payload = build_review_payload(
        coordinator_result=_coord(results=[_result_with_packet(metadata_packet)]),
        context=explicit,
    )
    assert payload["context"] == explicit


def test_empty_context_packet_in_metadata_is_omitted() -> None:
    """An empty dict packet should not surface as a context field."""

    payload = build_review_payload(
        coordinator_result=_coord(results=[_result_with_packet({})])
    )
    assert "context" not in payload


def test_first_metadata_packet_wins_when_multiple_agents_emit() -> None:
    first = {"narrative": "first"}
    second = {"narrative": "second"}
    payload = build_review_payload(
        coordinator_result=_coord(
            results=[
                _result_with_packet(first, agent_name="context_packager"),
                _result_with_packet(second, agent_name="hypothetical_other"),
            ]
        )
    )
    assert payload["context"] == first


# -- Suggested-reviewers routing from agent metadata ----------------------


def _result_with_reviewers(
    reviewers: list[dict],
    agent_name: str = "reviewer_router",
    findings: list[Finding] | None = None,
) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        findings=list(findings or []),
        metadata={"suggested_reviewers": reviewers},
    )


def test_suggested_reviewers_in_metadata_promoted_to_top_level() -> None:
    """Router emits via ``metadata['suggested_reviewers']`` — formatter routes it."""

    reviewers = [
        {"rank": 1, "login": "alice", "score": 0.75, "rationale": "codeowner"},
        {"rank": 2, "login": "bob", "score": 0.35, "rationale": "authorship"},
    ]
    payload = build_review_payload(
        coordinator_result=_coord(
            results=[_result_with_reviewers(reviewers)]
        )
    )
    assert payload["suggested_reviewers"] == reviewers


def test_explicit_suggested_reviewers_override_wins_over_metadata() -> None:
    """Caller-supplied kwarg takes precedence over agent metadata."""

    metadata_reviewers = [
        {"rank": 1, "login": "alice", "score": 0.75, "rationale": "codeowner"}
    ]
    explicit = [{"rank": 1, "login": "overridden", "score": 0.99, "rationale": "explicit"}]
    payload = build_review_payload(
        coordinator_result=_coord(
            results=[_result_with_reviewers(metadata_reviewers)]
        ),
        suggested_reviewers=explicit,
    )
    assert payload["suggested_reviewers"] == explicit


def test_empty_suggested_reviewers_in_metadata_is_omitted() -> None:
    """An empty list in metadata should not surface in the payload."""

    payload = build_review_payload(
        coordinator_result=_coord(
            results=[_result_with_reviewers([])]
        )
    )
    assert "suggested_reviewers" not in payload


def test_first_metadata_reviewer_list_wins_when_multiple_agents_emit() -> None:
    first = [{"rank": 1, "login": "alice", "score": 0.5, "rationale": "a"}]
    second = [{"rank": 1, "login": "bob", "score": 0.5, "rationale": "b"}]
    payload = build_review_payload(
        coordinator_result=_coord(
            results=[
                _result_with_reviewers(first, agent_name="reviewer_router"),
                _result_with_reviewers(second, agent_name="hypothetical_other"),
            ]
        )
    )
    assert payload["suggested_reviewers"] == first


def test_reviewer_suggestion_findings_excluded_from_conflicts() -> None:
    """``kind='reviewer_suggestion'`` findings ride on the suggested-reviewers
    channel, not the generic conflicts list. The formatter strips them."""

    suggestion = _finding(
        "info",
        kind="reviewer_suggestion",
        title="Suggested reviewer #1: @alice",
        detail="CODEOWNERS match.",
    )
    real_conflict = _finding(
        "warning",
        kind="convention",
        title="ADR-002 drift",
        detail="tuple return",
    )
    reviewers = [{"rank": 1, "login": "alice", "score": 0.6, "rationale": "CODEOWNERS match."}]
    router_result = AgentResult(
        agent_name="reviewer_router",
        findings=[suggestion],
        metadata={"suggested_reviewers": reviewers},
    )
    payload = build_review_payload(
        coordinator_result=_coord(
            results=[_result(real_conflict), router_result]
        )
    )
    # The reviewer_suggestion finding is NOT in conflicts.
    kinds = {c["kind"] for c in payload["conflicts"]}
    assert "reviewer_suggestion" not in kinds
    # The real conflict is still there.
    assert any(c["title"] == "ADR-002 drift" for c in payload["conflicts"])
    # Suggested reviewers routed to the top-level field.
    assert payload["suggested_reviewers"] == reviewers


def test_suggested_reviewers_survive_when_no_other_findings() -> None:
    """Edge case: router ran, nothing else did. We should still get
    a well-formed payload with suggested_reviewers populated and an
    empty conflicts list."""

    reviewers = [{"rank": 1, "login": "alice", "score": 0.6, "rationale": "r"}]
    suggestion = _finding(
        "info",
        kind="reviewer_suggestion",
        title="Suggested reviewer #1: @alice",
        detail="r",
    )
    router_result = AgentResult(
        agent_name="reviewer_router",
        findings=[suggestion],
        metadata={"suggested_reviewers": reviewers},
    )
    payload = build_review_payload(
        coordinator_result=_coord(results=[router_result])
    )
    assert payload["conflicts"] == []
    assert payload["suggested_reviewers"] == reviewers
