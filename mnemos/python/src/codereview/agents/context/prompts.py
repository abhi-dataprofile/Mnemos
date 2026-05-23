"""Structured-output schema for the Context Packager summary prompt.

Kept alongside :mod:`codereview.agents.conflict.prompts` in spirit: the
schema lives next to the agent that uses it so "what does this agent ask
the LLM to produce?" is a one-file answer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ContextSummary(BaseModel):
    """Output of :file:`context_summary.v1.md`.

    One 50-80 word paragraph. The model is instructed to skip meta
    commentary ("this packet contains...") and go straight to the most
    load-bearing context the reviewer needs — a related PR that
    contradicted the current approach, an ADR that constrains the
    design, a recently reverted file that should give the reviewer
    pause.
    """

    summary: str = Field(
        min_length=1,
        description=(
            "One short paragraph (target 50-80 words) summarising the "
            "assembled context for a reviewer about to read the diff. "
            "Favour actionable pointers over meta commentary; name "
            "specific ADRs, PR numbers, or files when they are the "
            "interesting signal."
        ),
    )
