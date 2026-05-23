"""Initial schema.

Creates every node and edge table from ``docs/architecture.md`` §3 and the
pgvector similarity indexes. The `pgvector` extension is enabled here so
a fresh Postgres comes up migrable.

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-04-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # -- Nodes ---------------------------------------------------------------

    op.create_table(
        "repositories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("github_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.BigInteger, nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=False, server_default="main"),
        sa.Column("last_indexed_sha", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "persons",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("github_login", sa.String(255), nullable=False, unique=True),
        sa.Column("github_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
    )

    op.create_table(
        "files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("language", sa.String(64), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("first_seen_sha", sa.String(64), nullable=False),
        sa.Column("last_seen_sha", sa.String(64), nullable=False),
        sa.UniqueConstraint("repository_id", "path", "content_hash", name="uq_files_path_hash"),
    )
    op.create_index("ix_files_repo_path", "files", ["repository_id", "path"])

    op.create_table(
        "symbols",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("qualified_name", sa.Text, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("signature", sa.Text, nullable=True),
        sa.Column("ast_hash", sa.String(64), nullable=False),
        sa.Column("start_line", sa.Integer, nullable=False),
        sa.Column("end_line", sa.Integer, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
    )
    op.create_index("ix_symbols_repo_qname", "symbols", ["repository_id", "qualified_name"])
    op.create_index("ix_symbols_ast_hash", "symbols", ["ast_hash"])

    op.create_table(
        "commits",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column(
            "author_id",
            UUID(as_uuid=True),
            sa.ForeignKey("persons.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("message", sa.Text, nullable=False, server_default=""),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repository_id", "sha", name="uq_commits_repo_sha"),
    )
    op.create_index("ix_commits_committed_at", "commits", ["committed_at"])

    op.create_table(
        "pull_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("state", sa.String(32), nullable=False, server_default="open"),
        sa.Column(
            "author_id",
            UUID(as_uuid=True),
            sa.ForeignKey("persons.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.UniqueConstraint("repository_id", "number", name="uq_prs_repo_number"),
    )

    op.create_table(
        "reviews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pull_request_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("persons.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "review_comments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "review_id",
            UUID(as_uuid=True),
            sa.ForeignKey("reviews.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("line", sa.Integer, nullable=True),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "adrs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="proposed"),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
    )

    op.create_table(
        "issues",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("state", sa.String(32), nullable=False, server_default="open"),
        sa.UniqueConstraint("repository_id", "number", name="uq_issues_repo_number"),
    )

    # -- Edges ---------------------------------------------------------------

    op.create_table(
        "symbol_calls",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "caller_id",
            UUID(as_uuid=True),
            sa.ForeignKey("symbols.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "callee_id",
            UUID(as_uuid=True),
            sa.ForeignKey("symbols.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line", sa.Integer, nullable=False),
        sa.Column("dynamic", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_symbol_calls_callee", "symbol_calls", ["callee_id"])
    op.create_index("ix_symbol_calls_caller", "symbol_calls", ["caller_id"])

    op.create_table(
        "symbol_imports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "importer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "imported_id",
            UUID(as_uuid=True),
            sa.ForeignKey("symbols.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False, server_default="import"),
        sa.Column("lazy", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("raw", sa.Text, nullable=False, server_default=""),
    )
    op.create_index("ix_symbol_imports_importer", "symbol_imports", ["importer_id"])

    op.create_table(
        "commit_modifies_file",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "commit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("commits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("change_kind", sa.String(32), nullable=False),
        sa.Column("prior_path", sa.Text, nullable=True),
    )
    op.create_index("ix_cmf_file", "commit_modifies_file", ["file_id"])

    op.create_table(
        "commit_modifies_symbol",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "commit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("commits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "symbol_id",
            UUID(as_uuid=True),
            sa.ForeignKey("symbols.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("change_kind", sa.String(32), nullable=False),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_cms_symbol", "commit_modifies_symbol", ["symbol_id"])

    op.create_table(
        "pr_contains_commit",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pull_request_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "commit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("commits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("pull_request_id", "commit_id", name="uq_pr_commit"),
    )

    op.create_table(
        "pr_references_issue",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pull_request_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issue_id",
            UUID(as_uuid=True),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("pull_request_id", "issue_id", name="uq_pr_issue"),
    )

    op.create_table(
        "file_references_adr",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "adr_id",
            UUID(as_uuid=True),
            sa.ForeignKey("adrs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("file_id", "adr_id", name="uq_file_adr"),
    )

    # -- pgvector similarity indexes ----------------------------------------
    # IVFFlat with cosine distance ops. `lists=100` is fine for repos up to
    # ~100k symbols; revisit when we ship per-repo tuning.

    op.execute(
        "CREATE INDEX ix_symbols_embedding ON symbols "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX ix_pull_requests_embedding ON pull_requests "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX ix_adrs_embedding ON adrs "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50)"
    )


def downgrade() -> None:
    for ix in [
        "ix_adrs_embedding",
        "ix_pull_requests_embedding",
        "ix_symbols_embedding",
    ]:
        op.execute(f"DROP INDEX IF EXISTS {ix}")

    for table in [
        "file_references_adr",
        "pr_references_issue",
        "pr_contains_commit",
        "commit_modifies_symbol",
        "commit_modifies_file",
        "symbol_imports",
        "symbol_calls",
        "issues",
        "adrs",
        "review_comments",
        "reviews",
        "pull_requests",
        "commits",
        "symbols",
        "files",
        "persons",
        "repositories",
    ]:
        op.drop_table(table)
