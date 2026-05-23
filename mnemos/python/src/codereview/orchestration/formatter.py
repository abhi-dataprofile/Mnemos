"""Shape agent output into the callback JSON the TS side expects.

The TypeScript callback consumes a :data:`Review` object (see
``typescript/src/formatters/reviewComment.ts``) with this shape::

    {
      "summary":              str | None,
      "conflicts":            list[Conflict],
      "context":              ContextPacket | None,
      "suggested_reviewers":  list[SuggestedReviewer],
      "failed_agents":        list[str]
    }

This module owns the Python-side translation so every agent can stay
ignorant of the wire format. Keeping the formatter pure (no I/O, no
GitHub calls) means we can unit-test the JSON exhaustively and
snapshot-test drift from the TS contract.

Design rules:

- The output is a plain ``dict[str, Any]`` so the callback sender can
  ``json.dumps`` it and sign it with HMAC without further adaptation.
- Fields that are empty are either omitted or emitted as ``None`` in a
  way that matches the TS optional semantics.
- Sorting and dedup live here, not in individual agents: the reviewer
  always sees conflicts in ``blocking → warning → info`` order.
"""

from __future__ import annotations

from typing import Any, Literal

from codereview import __version__
from codereview.agents.base import AgentResult, Finding
from codereview.orchestration.coordinator import CoordinatorResult

__all__ = [
    "build_review_payload",
    "summarize",
]


# Severity sort order (matches the TS formatter).
_SEVERITY_ORDER: dict[str, int] = {"blocking": 0, "warning": 1, "info": 2}

# Kinds of findings whose presence should change the review's top-line
# summary ("looks good" vs "blocking issue"). Keep aligned with severity
# labels rather than free-form agent kind strings.
_BlockingLevel = Literal["blocking", "warning", "info"]


def build_review_payload(
    *,
    coordinator_result: CoordinatorResult,
    summary: str | None = None,
    context: dict[str, Any] | None = None,
    suggested_reviewers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the JSON-serializable ``review`` object for the TS callback.

    Parameters
    ----------
    coordinator_result:
        The :class:`CoordinatorResult` from :class:`Coordinator.run`.
    summary:
        Optional prose summary. When ``None`` the formatter synthesises
        one from the conflict counts (:func:`summarize`).
    context:
        Optional context packet override. When ``None`` (the common
        case), the formatter looks for a ``context_packet`` entry on
        any successful agent's :attr:`AgentResult.metadata` and
        promotes it to the top-level ``context`` field. The Context
        Packager (Phase 5) emits the packet via this metadata channel
        so agents stay ignorant of the wire format.
    suggested_reviewers:
        Optional suggested-reviewer list from the Reviewer Router
        (Phase 6). Phase 4 passes ``None``.

    Returns
    -------
    A plain dict ready for ``json.dumps``. Keys match the TS ``Review``
    interface.
    """

    successful: list[AgentResult] = coordinator_result.successful
    # ``reviewer_suggestion`` findings are promoted out of the generic
    # conflict list so the PR comment renders them as an @-mention
    # block rather than inline with the conflicts. Strip them here
    # before collect_findings sorts and dedups the rest.
    findings = _collect_findings(successful, exclude_kind="reviewer_suggestion")

    payload: dict[str, Any] = {
        "summary": summary if summary is not None else summarize(findings),
        "conflicts": [_finding_to_conflict(f) for f in findings],
    }
    resolved_context = (
        context if context is not None else _context_from_metadata(successful)
    )
    if resolved_context:
        payload["context"] = resolved_context

    resolved_reviewers = (
        list(suggested_reviewers)
        if suggested_reviewers is not None
        else _reviewers_from_metadata(successful)
    )
    if resolved_reviewers:
        payload["suggested_reviewers"] = resolved_reviewers

    failed = _failed_agent_names(coordinator_result)
    if failed:
        payload["failed_agents"] = failed

    # Stamp the version so the TS-side footer can render "Reviewed by
    # Mnemos X.Y.Z" — easier bug correlation when self-hosters file
    # issues against a specific release.
    payload["mnemos_version"] = __version__

    return payload


def summarize(findings: list[Finding]) -> str:
    """Produce a one-line prose summary from the aggregated findings.

    Phase 4's summary is intentionally blunt. Phase 8 can swap in an
    LLM-written summary once we have enough real-world output to know
    what's worth saying.
    """

    if not findings:
        return "Mnemos ran three checks against this PR and found nothing to flag."

    counts = _counts_by_severity(findings)
    parts: list[str] = []
    for sev, word in (("blocking", "blocking"), ("warning", "warning"), ("info", "info")):
        n = counts.get(sev, 0)
        if n:
            parts.append(f"{n} {word}")
    return "Mnemos flagged " + ", ".join(parts) + "."


# -- Internals --------------------------------------------------------------


def _collect_findings(
    results: list[AgentResult],
    *,
    exclude_kind: str | None = None,
) -> list[Finding]:
    """Flatten + sort + dedup across agents.

    Each agent already dedupes its own output (see
    :func:`ConflictDetector._dedup`); here we stitch them together and
    re-dedup across agents in case two agents happen to surface the
    same issue (unlikely today, plausible once more agents land).

    ``exclude_kind`` filters out findings of a given kind before dedup.
    Phase 6's Reviewer Router uses this to keep its suggestions out of
    the conflict list (they render as a separate block in the TS
    comment formatter).
    """

    seen: set[tuple[str, str, str]] = set()
    merged: list[Finding] = []
    for result in results:
        for f in result.findings:
            if exclude_kind is not None and f.kind == exclude_kind:
                continue
            key = (
                f.kind,
                f.title,
                f.locations[0].path if f.locations else "",
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(f)

    merged.sort(key=_severity_sort_key)
    return merged


def _severity_sort_key(f: Finding) -> tuple[int, str]:
    # Unknown severities sort last (TS formatter behaves the same way).
    return (_SEVERITY_ORDER.get(f.severity, len(_SEVERITY_ORDER)), f.title)


def _finding_to_conflict(f: Finding) -> dict[str, Any]:
    """Map a :class:`Finding` into the TS ``Conflict`` shape.

    Pydantic models on both sides use the same field names, so we could
    ``model_dump(by_alias=True)``. We do this by hand to keep the seam
    explicit — a rename on the Python side should force a matching
    update here (and in the contract fixtures) rather than silently
    reshaping the wire payload.
    """

    out: dict[str, Any] = {
        "severity": f.severity,
        "kind": f.kind,
        "title": f.title,
        "detail": f.detail,
    }
    if f.locations:
        out["locations"] = [
            {"path": loc.path, **({"line": loc.line} if loc.line is not None else {})}
            for loc in f.locations
        ]
    if f.related_symbols:
        out["related_symbols"] = list(f.related_symbols)
    if f.suggested_action:
        out["suggested_action"] = f.suggested_action
    return out


def _counts_by_severity(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _failed_agent_names(result: CoordinatorResult) -> list[str]:
    names: list[str] = []
    for outcome in result.outcomes:
        if outcome.result is None:
            names.append(outcome.agent_name)
    return names


def _context_from_metadata(results: list[AgentResult]) -> dict[str, Any] | None:
    """Pull a ``context_packet`` out of any successful agent's metadata.

    The Context Packager (Phase 5) emits its packet this way so agents
    never have to know the TS wire shape. If more than one agent emits
    a packet (unlikely in v0.1) the first wins; we do not attempt to
    merge packets.
    """

    for result in results:
        packet = result.metadata.get("context_packet")
        if isinstance(packet, dict) and packet:
            return packet
    return None


def _reviewers_from_metadata(
    results: list[AgentResult],
) -> list[dict[str, Any]]:
    """Pull suggested reviewers out of any successful agent's metadata.

    The Reviewer Router (Phase 6) emits its top-3 list this way so the
    formatter can promote it to the top-level ``suggested_reviewers``
    field. If more than one agent emits a list, the first non-empty
    one wins — we don't attempt to merge across agents.
    """

    for result in results:
        candidates = result.metadata.get("suggested_reviewers")
        if isinstance(candidates, list) and candidates:
            return [c for c in candidates if isinstance(c, dict)]
    return []
