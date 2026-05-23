"""Coordinator error isolation behavior."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from codereview.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    Finding,
    PullRequestSnapshot,
)
from codereview.orchestration.coordinator import Coordinator


def _ctx() -> AgentContext:
    return AgentContext(
        pr=PullRequestSnapshot(
            number=1,
            title="t",
            author="abhi",
            head_sha="a" * 40,
            base_sha="b" * 40,
        ),
        repo_id=uuid4(),
        graph=object(),  # unused in these tests
        llm=object(),
    )


class _Happy(BaseAgent):
    name = "happy"
    description = "returns one info finding"
    version = "0"

    async def run(self, ctx: AgentContext) -> AgentResult:
        return AgentResult(
            agent_name=self.name,
            findings=[Finding(severity="info", kind="x", title="t", detail="d")],
        )


class _Crashy(BaseAgent):
    name = "crashy"
    description = "raises"
    version = "0"

    async def run(self, ctx: AgentContext) -> AgentResult:
        raise RuntimeError("boom")


class _Slow(BaseAgent):
    name = "slow"
    description = "sleeps past the timeout"
    version = "0"

    async def run(self, ctx: AgentContext) -> AgentResult:
        await asyncio.sleep(5)
        return AgentResult(agent_name=self.name)


@pytest.mark.asyncio
async def test_isolates_crashes_and_timeouts() -> None:
    coordinator = Coordinator([_Happy(), _Crashy(), _Slow()], per_agent_timeout_s=0.05)
    result = await coordinator.run(_ctx())

    by_name = {o.agent_name: o for o in result.outcomes}
    assert by_name["happy"].result is not None
    assert by_name["happy"].error is None
    assert by_name["crashy"].result is None
    assert "boom" in by_name["crashy"].error  # type: ignore[operator]
    assert by_name["slow"].timed_out is True
    # Successful results survive regardless.
    assert len(result.successful) == 1
