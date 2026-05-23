"""Unit tests for :mod:`codereview.agents.context.summary`."""

from __future__ import annotations

from typing import Any

from codereview.agents.base import PullRequestSnapshot
from codereview.agents.context.prompts import ContextSummary
from codereview.agents.context.summary import summarise_packet
from codereview.agents.context.types import (
    ContextPacket,
    LinkedIssue,
    RecentCommit,
    RelatedADR,
    RelatedPR,
)


def _pr(*, body: str = "") -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=99,
        title="Tighten retry policy",
        body=body,
        author="abhi",
        head_sha="h" * 40,
        base_sha="b" * 40,
    )


def _packet_with_content() -> ContextPacket:
    return ContextPacket(
        related_prs=[RelatedPR(number=12, title="prior retry tweak", score=0.6)],
        related_adrs=[RelatedADR(title="ADR-007 idempotency")],
        recent_commits=[
            RecentCommit(
                sha="abc1234567",
                title="bump backoff",
                author_login="alice",
                file_path="retry.py",
            )
        ],
        linked_issues=[
            LinkedIssue(kind="github", identifier="#3", number=3, title="flaky retries", state="open")
        ],
        risk_notes=["High churn: retry.py had 25 commits in the last 30 days."],
        narrative="",
    )


class _SuccessLLM:
    """Returns a canned ContextSummary."""

    def __init__(self, text: str = "Reviewer-friendly summary.") -> None:
        self._text = text
        self.last_kwargs: dict[str, Any] | None = None

    async def structured_call(self, **kwargs: Any) -> ContextSummary:
        self.last_kwargs = kwargs
        return ContextSummary(summary=self._text)


class _RaisingLLM:
    async def structured_call(self, **_kwargs: Any) -> ContextSummary:
        raise RuntimeError("anthropic 503")


# -- Empty packet ---------------------------------------------------------


async def test_empty_packet_returns_empty_string() -> None:
    out = await summarise_packet(
        llm=_SuccessLLM(),
        pr=_pr(),
        packet=ContextPacket(),
    )
    assert out == ""


# -- LLM success path ----------------------------------------------------


async def test_llm_success_returns_summary_text() -> None:
    llm = _SuccessLLM(text="A crisp 30-second briefing.")
    out = await summarise_packet(
        llm=llm,
        pr=_pr(body="The body."),
        packet=_packet_with_content(),
    )
    assert out == "A crisp 30-second briefing."


async def test_llm_success_strips_whitespace() -> None:
    llm = _SuccessLLM(text="   trimmed me  \n")
    out = await summarise_packet(
        llm=llm,
        pr=_pr(),
        packet=_packet_with_content(),
    )
    assert out == "trimmed me"


async def test_llm_call_passes_prompt_version_and_schema() -> None:
    llm = _SuccessLLM()
    await summarise_packet(
        llm=llm, pr=_pr(), packet=_packet_with_content()
    )
    assert llm.last_kwargs is not None
    assert llm.last_kwargs["output_schema"] is ContextSummary
    # Loader records ``<name>.<version>`` so usage counters stay unambiguous.
    assert llm.last_kwargs["prompt_version"] == "context_summary.v1"
    assert "system" in llm.last_kwargs


async def test_rendered_prompt_includes_packet_data() -> None:
    llm = _SuccessLLM()
    await summarise_packet(
        llm=llm, pr=_pr(body="Body text."), packet=_packet_with_content()
    )
    rendered = llm.last_kwargs["prompt"]
    # Spot-check that key facts make it into the prompt body.
    assert "Tighten retry policy" in rendered
    assert "Body text." in rendered
    assert "#12" in rendered
    assert "ADR-007 idempotency" in rendered
    assert "abc1234" in rendered
    assert "#3" in rendered
    assert "High churn" in rendered


async def test_missing_body_renders_no_description_placeholder() -> None:
    llm = _SuccessLLM()
    await summarise_packet(llm=llm, pr=_pr(body=""), packet=_packet_with_content())
    assert "(no description)" in llm.last_kwargs["prompt"]


# -- Fallback paths ------------------------------------------------------


async def test_llm_error_falls_back_to_deterministic_string() -> None:
    out = await summarise_packet(
        llm=_RaisingLLM(),
        pr=_pr(),
        packet=_packet_with_content(),
    )
    assert "Packet assembled" in out
    assert "1 related PR(s)" in out


async def test_llm_without_structured_call_uses_fallback() -> None:
    class _Bare:
        pass

    out = await summarise_packet(
        llm=_Bare(),
        pr=_pr(),
        packet=_packet_with_content(),
    )
    assert "Packet assembled" in out


async def test_fallback_returns_empty_when_packet_truly_empty() -> None:
    """If LLM dies *and* the packet has no items, narrative is empty."""

    out = await summarise_packet(
        llm=_RaisingLLM(),
        pr=_pr(),
        packet=ContextPacket(narrative="seed"),  # narrative ignored by is_empty
    )
    # narrative=seed makes is_empty False, so we DO call fallback,
    # but fallback finds no list items so returns "".
    assert out == ""
