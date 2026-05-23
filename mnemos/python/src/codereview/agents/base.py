"""Agent framework contracts.

Every agent Mnemos runs subclasses :class:`BaseAgent`. The shapes here are
stable and form the Phase 1 public surface; changes to these types are
schema changes and go in ``docs/architecture.md``.

See ``docs/writing-an-agent.md`` for a narrative walkthrough.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# -- Findings ----------------------------------------------------------------


Severity = Literal["blocking", "warning", "info"]


class Location(BaseModel):
    """A file and optional line range pointed to by a finding."""

    path: str
    line: int | None = None
    end_line: int | None = None


class Finding(BaseModel):
    """One observation from an agent. Rendered as a bullet in the PR comment."""

    severity: Severity
    kind: str
    title: str
    detail: str
    locations: list[Location] = Field(default_factory=list)
    related_symbols: list[str] = Field(default_factory=list)
    suggested_action: str | None = None


# -- PR snapshot inputs ------------------------------------------------------


class ChangedFile(BaseModel):
    """A file modified in the PR."""

    path: str
    change_kind: Literal["added", "modified", "deleted", "renamed"]
    patch: str = ""
    language: str | None = None


class ChangedSymbol(BaseModel):
    """A symbol whose body or signature changed in the PR.

    ``old_source`` / ``new_source`` hold the symbol's definition text at
    base and head respectively. They are optional so cheaper snapshot
    producers (e.g. a summary mode) can omit them, but agents that want
    to run an AST classifier or an LLM semantic check need them populated.
    """

    qualified_name: str
    kind: Literal["function", "class", "method", "constant"]
    change_kind: Literal["added", "modified", "deleted", "renamed", "body_only"]
    old_ast_hash: str | None = None
    new_ast_hash: str | None = None
    old_signature: str | None = None
    new_signature: str | None = None
    old_source: str | None = None
    new_source: str | None = None
    file_path: str


class PullRequestSnapshot(BaseModel):
    """Frozen view of a PR at ``head_sha`` passed to every agent run."""

    number: int
    title: str
    body: str = ""
    author: str
    head_sha: str
    base_sha: str
    changed_files: list[ChangedFile] = Field(default_factory=list)
    changed_symbols: list[ChangedSymbol] = Field(default_factory=list)


# -- Agent IO ----------------------------------------------------------------


class AgentContext(BaseModel):
    """Everything an agent can read. Agents may not read anything else.

    The ``graph`` and ``llm`` clients are typed as ``Any`` at the Pydantic
    layer to avoid forward-reference rebuild ceremony and keep the agent
    module free of an import cycle with :mod:`codereview.graph.client` and
    :mod:`codereview.llm.client`. Concrete agent code should type its
    parameter as :class:`codereview.graph.client.GraphClient` and
    :class:`codereview.llm.client.LLMClient` via local imports.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    pr: PullRequestSnapshot
    repo_id: UUID
    graph: Any
    llm: Any
    config: dict = Field(default_factory=dict)
    workspace_root: Path | None = None
    """On-disk checkout of ``pr.head_sha`` that agents may read from.

    Populated by the Context Packager in production; set by tests
    pointing at a fixture. Agents must treat it as read-only and tolerate
    missing files (e.g. a symbol in the graph that the workspace no
    longer has on disk).
    """


class AgentResult(BaseModel):
    """What an agent returns. The coordinator aggregates these."""

    agent_name: str
    findings: list[Finding] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    tokens_used: int = 0
    wall_time_ms: int = 0


# -- BaseAgent ---------------------------------------------------------------


class BaseAgent(ABC):
    """Abstract base for every Mnemos agent.

    Subclasses set ``name``, ``description``, and ``version`` as class-level
    attributes and implement :meth:`run`.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    version: ClassVar[str]

    @abstractmethod
    async def run(self, ctx: AgentContext) -> AgentResult:
        """Produce findings for the PR described by ``ctx``."""
