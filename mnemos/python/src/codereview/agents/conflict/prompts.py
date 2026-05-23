"""Structured-output schemas for Conflict Detector LLM prompts.

Each prompt in ``codereview/llm/prompts`` names one of the classes defined
here via the ``output_schema`` frontmatter field. Keeping them together
makes it easy to audit "what does this agent ask the LLM to produce?" —
and keeps the prompt loader's dynamic ``importlib`` targets in a single,
obvious place.

Conventions:
- Keep the fields minimal and typed. Enums/Literals where possible so
  prompt drift surfaces as a Pydantic validation error, not as a quiet
  string match miss downstream.
- Every string field has a non-empty docstring because the JSON schema
  exposed to Claude via tool-use is derived from the Pydantic model; the
  docstrings become ``description`` in the schema and directly influence
  the model's output quality.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SemanticCheckResult(BaseModel):
    """Output of :file:`semantic_conflict_check.v1.md`.

    The LLM reads the before/after signature of a changed symbol plus one
    caller snippet and decides whether the caller is still compatible.
    """

    compatible: bool = Field(
        description=(
            "True if the caller still works unchanged against the new "
            "signature; False if the signature change breaks the caller."
        )
    )
    reason: str = Field(
        min_length=1,
        description=(
            "One or two sentences explaining the judgement. If "
            "compatible is False, name the specific parameter, type, or "
            "return shape that broke."
        ),
    )
    suggested_fix: str | None = Field(
        default=None,
        description=(
            "Concrete one-line instruction the PR author could apply at "
            "the caller site (e.g. 'pass repo.get_by_id(id) instead of "
            "id'). Omit when compatible is True."
        ),
    )


ADRCheckSeverity = Literal["warning", "info"]


class ADRCheckResult(BaseModel):
    """Output of :file:`adr_contradiction_check.v1.md`.

    Given a PR diff summary and one accepted ADR, decide whether the PR
    contradicts the decision. Severity is capped at ``warning`` — ADR
    conflicts are never auto-blocking in v0.1; the reviewer still owns
    the call.
    """

    contradicts: bool = Field(
        description=(
            "True if the PR takes an action the ADR explicitly rejected "
            "or violates a constraint the ADR enshrined."
        )
    )
    reasoning: str = Field(
        min_length=1,
        description=(
            "Explain the conflict (or the lack of one). Quote the ADR "
            "clause when possible so the PR author can audit the call."
        ),
    )
    severity: ADRCheckSeverity = Field(
        default="warning",
        description=(
            "'warning' when the PR clearly contradicts an accepted ADR; "
            "'info' when the relationship is worth surfacing but the "
            "contradiction is partial or context-dependent."
        ),
    )
