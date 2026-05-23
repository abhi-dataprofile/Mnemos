"""Agent registry.

Phase 1 ships the registry empty. Phases 4-6 populate it with the three
v0.1 agents. Contributors register new agents here per
``docs/writing-an-agent.md``.
"""

from __future__ import annotations

from collections.abc import Iterable

from codereview.agents.base import BaseAgent

# Populated as agents are written. Kept as a dict so lookup by name is O(1)
# and the order is explicit.
AGENTS: dict[str, type[BaseAgent]] = {}


def enabled_agents(enabled_names: Iterable[str]) -> list[type[BaseAgent]]:
    """Return the registered agent classes matching ``enabled_names``.

    Unknown names are silently skipped. This keeps misconfiguration from
    crashing the service; the coordinator logs unknown agents on startup.
    """

    seen: set[str] = set()
    out: list[type[BaseAgent]] = []
    for name in enabled_names:
        if name in AGENTS and name not in seen:
            out.append(AGENTS[name])
            seen.add(name)
    return out
