"""``python -m codereview.tasks.index_repo`` — index a repo on disk.

Wraps :class:`codereview.graph.builder.GraphBuilder` so a developer can
populate the memory graph for any repo checkout without going through the
GitHub App. Used to gate the Phase 2 acceptance criterion (the fixture
should index in under two minutes on a laptop).

The tool is intentionally minimal:

- One required flag, ``--path``, the working-tree root to index.
- ``--owner`` / ``--name`` default to ``local`` / the directory name.
- ``--github-id`` and ``--installation-id`` default to 0 — they're only
  meaningful when the repo will later be claimed by a real GitHub install.
- Embeddings are skipped by default. Pass ``--with-embeddings`` to opt in;
  this requires the relevant API keys in the environment and is not needed
  for graph-shape verification.

The Repository row is upserted on ``(owner, name)`` so re-running the CLI
against the same checkout reuses the existing row. Inside that row,
``GraphBuilder``'s own dedup keeps Files / Symbols / ADRs idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from sqlalchemy import select

from codereview.db import dispose_engine, get_session_factory
from codereview.graph import models as m
from codereview.graph.builder import BuildStats, GraphBuilder

_log = logging.getLogger("codereview.tasks.index_repo")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codereview.tasks.index_repo",
        description="Populate the Mnemos memory graph from a repository checkout.",
    )
    parser.add_argument(
        "--path",
        required=True,
        type=Path,
        help="Path to the repository checkout to index.",
    )
    parser.add_argument(
        "--owner",
        default="local",
        help="Repository owner used for the Repository row (default: local).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Repository name; defaults to the basename of --path.",
    )
    parser.add_argument(
        "--github-id",
        type=int,
        default=0,
        help="GitHub repository ID for the Repository row (default: 0).",
    )
    parser.add_argument(
        "--installation-id",
        type=int,
        default=0,
        help="GitHub installation ID for the Repository row (default: 0).",
    )
    parser.add_argument(
        "--default-branch",
        default="main",
        help="Default branch label for the Repository row (default: main).",
    )
    parser.add_argument(
        "--head-sha",
        default="working-tree",
        help="SHA stamped on every File row this run touches (default: working-tree).",
    )
    parser.add_argument(
        "--with-embeddings",
        action="store_true",
        help=(
            "Opt in to embedding submission. Requires API keys; off by default so "
            "graph-shape testing stays free."
        ),
    )
    return parser


async def _upsert_repository(
    session,  # type: ignore[no-untyped-def]
    *,
    owner: str,
    name: str,
    github_id: int,
    installation_id: int,
    default_branch: str,
) -> m.Repository:
    """Find or create a Repository row keyed on (owner, name)."""
    existing = (
        await session.execute(
            select(m.Repository).where(m.Repository.owner == owner, m.Repository.name == name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    repo = m.Repository(
        github_id=github_id,
        owner=owner,
        name=name,
        installation_id=installation_id,
        default_branch=default_branch,
    )
    session.add(repo)
    await session.flush()
    return repo


def _resolve_repo_root(raw_path: Path) -> Path:
    """Resolve and validate ``--path`` synchronously before the async run."""
    repo_root = raw_path.resolve()
    if not repo_root.is_dir():
        raise SystemExit(f"--path is not a directory: {repo_root}")
    return repo_root


async def _run(args: argparse.Namespace) -> BuildStats:
    repo_root = _resolve_repo_root(args.path)
    name = args.name or repo_root.name
    factory = get_session_factory()

    started = time.monotonic()
    async with factory() as session:
        repo_row = await _upsert_repository(
            session,
            owner=args.owner,
            name=name,
            github_id=args.github_id,
            installation_id=args.installation_id,
            default_branch=args.default_branch,
        )

        if args.with_embeddings:
            # Wiring real providers belongs in a separate plumbing PR; for
            # now we surface the request loudly so users know to plumb it.
            raise SystemExit(
                "--with-embeddings is not yet wired to real providers; "
                "leave it off to index the graph shape only."
            )

        builder = GraphBuilder(session, repo_row.id, embedding_pipeline=None)
        stats = await builder.index_working_tree(repo_root, head_sha=args.head_sha)
        await session.commit()

    elapsed = time.monotonic() - started
    _log.info(
        "indexed repo=%s/%s in %.2fs files=%d symbols=%d edges=%d adrs=%d "
        "dropped_dynamic=%d dropped_unresolved=%d",
        args.owner,
        name,
        elapsed,
        stats.files_indexed,
        stats.symbols_written,
        stats.call_edges_written,
        stats.adrs_written,
        stats.dynamic_calls_dropped,
        stats.unresolved_calls_dropped,
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = _build_arg_parser().parse_args(argv)
    try:
        stats = asyncio.run(_run(args))
    finally:
        # Make sure the engine pool is closed even on error paths so the
        # process exits cleanly under CI / scripts.
        asyncio.run(dispose_engine())
    print(
        f"files={stats.files_indexed} symbols={stats.symbols_written} "
        f"edges={stats.call_edges_written} adrs={stats.adrs_written}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
