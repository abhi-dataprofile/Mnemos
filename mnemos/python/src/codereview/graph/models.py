"""SQLAlchemy ORM models for the memory graph.

These are read-side mappings used by :class:`codereview.graph.client.GraphClient`.
Schema DDL lives in Alembic migrations (``alembic/versions/001_initial_schema.py``)
rather than being emitted from ``Base.metadata.create_all``; the migration is
the authoritative spec and these models mirror it.

See ``docs/architecture.md`` §3 for the node/edge inventory.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from codereview.db import Base

# Embedding dimensions. Voyage voyage-code-3 and OpenAI text-embedding-3-large
# both emit 1024+ dims depending on config; we reserve 1536 to cover both.
EMBEDDING_DIM = 1536


# -- Nodes -------------------------------------------------------------------


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    last_indexed_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class File(Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("repository_id", "path", "content_hash"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_sha: Mapped[str] = mapped_column(String(64), nullable=False)


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    ast_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)


class Commit(Base):
    __tablename__ = "commits"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    sha: Mapped[str] = mapped_column(String(64), nullable=False)
    author_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text, default="")
    committed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("repository_id", "sha"),)


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    github_login: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), default="open")
    author_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL"), nullable=True
    )
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    merged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    __table_args__ = (UniqueConstraint("repository_id", "number"),)


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    pull_request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewComment(Base):
    __tablename__ = "review_comments"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    review_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class ADR(Base):
    __tablename__ = "adrs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    body: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), default="open")

    __table_args__ = (UniqueConstraint("repository_id", "number"),)


# -- Edges -------------------------------------------------------------------


class SymbolCall(Base):
    __tablename__ = "symbol_calls"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    caller_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    callee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    dynamic: Mapped[bool] = mapped_column(Boolean, default=False)


class SymbolImport(Base):
    __tablename__ = "symbol_imports"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    importer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    imported_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="import")
    lazy: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[str] = mapped_column(Text, default="")


class CommitModifiesFile(Base):
    __tablename__ = "commit_modifies_file"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    commit_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("commits.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    change_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    prior_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommitModifiesSymbol(Base):
    __tablename__ = "commit_modifies_symbol"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    commit_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("commits.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    change_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class PRContainsCommit(Base):
    __tablename__ = "pr_contains_commit"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    pull_request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=False
    )
    commit_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("commits.id", ondelete="CASCADE"), nullable=False
    )


class PRReferencesIssue(Base):
    __tablename__ = "pr_references_issue"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    pull_request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=False
    )
    issue_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )


class FileReferencesADR(Base):
    __tablename__ = "file_references_adr"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    file_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    adr_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("adrs.id", ondelete="CASCADE"), nullable=False
    )


# Re-export for Alembic autogen convenience.
__all__ = [
    "ADR",
    "Commit",
    "CommitModifiesFile",
    "CommitModifiesSymbol",
    "File",
    "FileReferencesADR",
    "Issue",
    "PRContainsCommit",
    "PRReferencesIssue",
    "Person",
    "PullRequest",
    "Repository",
    "Review",
    "ReviewComment",
    "Symbol",
    "SymbolCall",
    "SymbolImport",
]
