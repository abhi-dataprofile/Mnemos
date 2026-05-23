"""Per-PR agent coordinator.

Runs enabled agents concurrently with per-agent timeouts, token budgets,
and error isolation. Phase 1 ships the skeleton; real LLM + graph calls
arrive as agents land.

Failure semantics (defended by Phase 4+ tests):

- A single agent crash becomes a metadata entry on the final review, not a
  failed review.
- Exceeding a per-agent token budget short-circuits that agent with a
  ``budget_exceeded`` flag, leaving others intact.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from codereview.agents.base import AgentContext, AgentResult, BaseAgent
from codereview.logging import get_logger
from codereview.metrics import AGENT_FAILURES, REVIEW_DURATION

_log = get_logger(__name__)


@dataclass(slots=True)
class AgentRunOutcome:
    agent_name: str
    result: AgentResult | None = None
    error: str | None = None
    timed_out: bool = False
    wall_time_ms: int = 0


@dataclass(slots=True)
class CoordinatorResult:
    outcomes: list[AgentRunOutcome] = field(default_factory=list)

    @property
    def successful(self) -> list[AgentResult]:
        return [o.result for o in self.outcomes if o.result is not None]


class Coordinator:
    """Run a set of agents against a single :class:`AgentContext`.

    Parameters
    ----------
    agents:
        Instantiated agents. The registry turns class refs into instances.
    per_agent_timeout_s:
        Wall-clock cap applied to each ``agent.run`` call.
    """

    def __init__(
        self,
        agents: list[BaseAgent],
        *,
        per_agent_timeout_s: float = 30.0,
    ) -> None:
        self._agents = agents
        self._timeout_s = per_agent_timeout_s

    async def run(self, ctx: AgentContext) -> CoordinatorResult:
        start = time.monotonic()
        outcomes = await asyncio.gather(
            *(self._run_one(agent, ctx) for agent in self._agents),
            return_exceptions=False,
        )
        result = CoordinatorResult(outcomes=list(outcomes))
        # Metrics: a review is "ok" when at least one agent produced a
        # result. Wall-clock here covers the coordinator only; the
        # analyze job adds queue + callback time on top.
        any_ok = any(o.result is not None for o in result.outcomes)
        REVIEW_DURATION.labels(status="ok" if any_ok else "failed").observe(
            time.monotonic() - start
        )
        return result

    async def _run_one(self, agent: BaseAgent, ctx: AgentContext) -> AgentRunOutcome:
        start = time.monotonic()
        outcome = AgentRunOutcome(agent_name=agent.name)
        try:
            result = await asyncio.wait_for(agent.run(ctx), timeout=self._timeout_s)
            outcome.result = result
        except asyncio.TimeoutError:  # noqa: UP041 - explicit on purpose
            # On 3.11+ ``asyncio.TimeoutError`` IS the builtin ``TimeoutError``
            # (alias). On earlier runtimes ``asyncio.wait_for`` raises the
            # asyncio-specific subclass, so catching the namespaced name is
            # portable. Project targets 3.11; this is belt-and-braces.
            outcome.timed_out = True
            AGENT_FAILURES.labels(agent=agent.name, reason="timeout").inc()
            _log.warning("agent_timeout", agent=agent.name, timeout_s=self._timeout_s)
        except Exception as exc:
            outcome.error = repr(exc)
            AGENT_FAILURES.labels(agent=agent.name, reason="exception").inc()
            _log.exception("agent_crashed", agent=agent.name)
        finally:
            outcome.wall_time_ms = int((time.monotonic() - start) * 1000)
        return outcome
