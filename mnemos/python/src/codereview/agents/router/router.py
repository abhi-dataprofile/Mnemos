"""ReviewerRouter agent.

Third agent. Ranks humans for review by blending graph signals
(CODEOWNERS, authorship, past reviews, call-graph adjacency, review
acceptance rate, current load) through a deterministic scoring
function. No LLM call — this agent is pure data, which makes it
cheap to run on every PR.

Wire surface:

- Emits one ``Finding`` per top-3 suggestion, with
  ``kind="reviewer_suggestion"`` and a stable per-finding
  ``title``/``detail`` shape the formatter recognises.
- Attaches ``rank``, ``score``, and ``login`` fields to the finding
  via :meth:`_suggested_reviewer_payload` which are promoted by the
  formatter into the ``suggested_reviewers`` wire array. Using
  findings as the carrier keeps the agent consistent with the
  existing aggregator, even though the formatter pulls these out
  ahead of the generic ``conflicts`` list.
- Populates :attr:`AgentResult.metadata` with per-candidate debug
  info so telemetry can inspect why each was (or wasn't) ranked.
"""

from __future__ import annotations

from typing import Any, ClassVar

from codereview.agents.base import AgentContext, AgentResult, BaseAgent, Finding
from codereview.agents.router.candidates import (
    assemble_candidates,
    gather_signals,
    load_codeowners_from_workspace,
)
from codereview.agents.router.rationale import rationale
from codereview.agents.router.score import score
from codereview.agents.router.types import Candidate, ScoredCandidate, Signals
from codereview.logging import get_logger

__all__ = ["ReviewerRouter"]

_log = get_logger(__name__)

# The plan says top-3. Exposed so tests can monkey-patch if needed and
# so a future config knob can land without touching the agent body.
DEFAULT_TOP_K = 3


class ReviewerRouter(BaseAgent):
    """Rank humans for review by blending expertise + capacity."""

    name: ClassVar[str] = "reviewer_router"
    description: ClassVar[str] = (
        "Ranks humans for review, balancing expertise and load. "
        "No LLM; pure graph-driven scoring."
    )
    version: ClassVar[str] = "0.1.0"

    async def run(self, ctx: AgentContext) -> AgentResult:
        metadata: dict[str, Any] = {"sections": {}}

        codeowners = load_codeowners_from_workspace(ctx.workspace_root)
        metadata["sections"]["codeowners"] = {
            "found": codeowners is not None,
            "entries": len(codeowners) if codeowners is not None else 0,
        }

        candidates = await assemble_candidates(ctx, codeowners=codeowners)
        metadata["sections"]["candidates"] = {"count": len(candidates)}
        if not candidates:
            return AgentResult(agent_name=self.name, findings=[], metadata=metadata)

        try:
            signals_by_login = await gather_signals(
                ctx, candidates, codeowners=codeowners
            )
        except Exception as exc:
            # Catastrophic failure of the signal layer shouldn't crash
            # the coordinator. Degrade to "no suggestions" with the
            # error captured in metadata for debugging.
            _log.warning("router.signals_error", error=repr(exc))
            metadata["sections"]["signals"] = {"error": repr(exc)}
            return AgentResult(agent_name=self.name, findings=[], metadata=metadata)

        scored = _score_all(candidates, signals_by_login)
        top = _top_k(scored, DEFAULT_TOP_K)

        metadata["sections"]["ranked"] = [
            {"login": sc.candidate.login, "score": round(sc.score, 4)}
            for sc in top
        ]

        findings: list[Finding] = []
        for rank, sc in enumerate(top, start=1):
            findings.append(_suggested_reviewer_finding(rank=rank, scored=sc))

        # Raw suggested-reviewer dicts ride on metadata so the
        # formatter can route them directly without re-parsing finding
        # text. Mirror of how the Context Packager emits
        # ``context_packet`` in metadata.
        metadata["suggested_reviewers"] = [
            _suggested_reviewer_payload(rank=rank, scored=sc)
            for rank, sc in enumerate(top, start=1)
        ]

        return AgentResult(agent_name=self.name, findings=findings, metadata=metadata)


# -- Internals -------------------------------------------------------------


def _score_all(
    candidates: list[Candidate],
    signals_by_login: dict[str, Signals],
) -> list[ScoredCandidate]:
    out: list[ScoredCandidate] = []
    for cand in candidates:
        sig = signals_by_login.get(cand.login, Signals())
        out.append(
            ScoredCandidate(candidate=cand, score=score(cand, sig), signals=sig)
        )
    return out


def _top_k(scored: list[ScoredCandidate], k: int) -> list[ScoredCandidate]:
    """Pick the top ``k`` by score, tiebreaking on login (stable).

    The secondary sort on login keeps test assertions deterministic
    when two candidates end up with the same score — something that
    happens on the small seeded graphs the unit tests use.
    """

    ranked = sorted(scored, key=lambda sc: (-sc.score, sc.candidate.login))
    # Drop candidates whose score is zero or negative — they are not
    # worth suggesting. A zero-score candidate matched no signal and
    # has no open-PR penalty; surfacing them would add noise.
    return [sc for sc in ranked if sc.score > 0][:k]


def _suggested_reviewer_finding(*, rank: int, scored: ScoredCandidate) -> Finding:
    login = scored.candidate.login
    detail = rationale(scored.candidate, scored.signals)
    return Finding(
        severity="info",
        kind="reviewer_suggestion",
        title=f"Suggested reviewer #{rank}: @{login}",
        detail=detail,
    )


def _suggested_reviewer_payload(
    *, rank: int, scored: ScoredCandidate
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "rank": rank,
        "login": scored.candidate.login,
        "score": round(scored.score, 4),
        "rationale": rationale(scored.candidate, scored.signals),
    }
    if scored.candidate.is_team:
        out["is_team"] = True
    return out
