"""HTTP request and response Pydantic schemas.

Matches the HTTP contract in ``docs/architecture.md`` §8. These are the
types the TypeScript service serializes into; schema drift here is a
breaking change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

# -- Shared sub-schemas ------------------------------------------------------


class RepositoryDescriptor(BaseModel):
    owner: str
    name: str
    github_id: int


class PullRequestDescriptor(BaseModel):
    number: int
    head_sha: str
    base_sha: str
    title: str
    body: str = ""
    author: str


# -- Request bodies ----------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Body of ``POST /v1/pull-requests/analyze``."""

    installation_id: int
    repository: RepositoryDescriptor
    pull_request: PullRequestDescriptor
    callback_url: HttpUrl
    callback_secret: str = Field(
        min_length=16, description="Per-job HMAC secret for the orchestrator callback."
    )


class IndexRequest(BaseModel):
    """Body of ``POST /v1/repositories/index``."""

    installation_id: int
    repository: RepositoryDescriptor
    depth: int = Field(default=1000, ge=1, le=10_000)


class IncrementalUpdateRequest(BaseModel):
    """Body of ``POST /v1/repositories/incremental-update``.

    Emitted by the TS push handler when the default branch advances. The
    orchestrator re-ingests the slice of history between ``base_sha`` and
    ``head_sha``.
    """

    installation_id: int
    repository: RepositoryDescriptor
    base_sha: str = Field(min_length=7, max_length=40)
    head_sha: str = Field(min_length=7, max_length=40)


# -- Responses ---------------------------------------------------------------


JobStatus = Literal["queued", "running", "completed", "failed"]


class JobAccepted(BaseModel):
    """202 body for enqueue endpoints."""

    job_id: str
    status: JobStatus = "queued"


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
