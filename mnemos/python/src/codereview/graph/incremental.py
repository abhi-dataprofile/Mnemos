"""Incremental repository re-indexer.

Where :class:`codereview.graph.builder.GraphBuilder` rebuilds the graph from
scratch, :class:`IncrementalUpdater` applies the minimum set of writes implied
by a git diff between ``last_indexed_sha`` and ``head_sha``.

Flow (matches Phase 2, task 3 in the plan):

1. Caller supplies the list of file changes (``A`` / ``M`` / ``D``) produced
   by ``git diff --name-status base..head``. Tests bypass the shell-out by
   constructing :class:`FileChange` objects directly; the
   :func:`diff_head_vs_base` helper wraps git for real runs.
2. For each **deleted** file, the row is removed from ``files``. FK cascade
   takes care of the associated ``symbols`` and ``symbol_calls``.
3. For each **added / modified** file, the file row is upserted in place
   (same primary key, new ``content_hash`` / ``last_seen_sha``) and the
   symbol set is reconciled on ``(file_id, qualified_name)``. Preserving
   ``File.id`` and ``Symbol.id`` when the qualified name stays the same
   means edges from unchanged files survive a body edit.
4. After all files are reindexed, ``symbol_calls`` outgoing from the changed
   files are recomputed: all edges whose caller lives in one of the changed
   files are deleted, then re-emitted from the fresh parse.
5. ``Repository.last_indexed_sha`` is bumped to ``head_sha``.

The updater does **not** try to cascade-fix edges from *unchanged* files that
referenced symbols deleted in this batch. Those edges are cascade-removed at
step 2 / 3 and can only be restored by a full reindex. v0.1 accepts this
tradeoff; profiling in Phase 3 will decide whether to maintain a reverse
index.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview.graph import models as m
from codereview.graph.adr import ADR_SEARCH_PATHS, parse_adr
from codereview.graph.embeddings import EmbeddingPipeline, EmbeddingRequest
from codereview.parsers.base import ImportRef, LanguageParser, Symbol
from codereview.parsers.registry import parser_for_path

ChangeKind = Literal["A", "M", "D"]


@dataclass(slots=True, frozen=True)
class FileChange:
    """One entry from ``git diff --name-status base..head``."""

    path: str
    kind: ChangeKind


@dataclass(slots=True)
class IncrementalStats:
    files_added: int = 0
    files_modified: int = 0
    files_removed: int = 0
    symbols_written: int = 0
    symbols_deleted: int = 0
    call_edges_written: int = 0
    dynamic_calls_dropped: int = 0
    unresolved_calls_dropped: int = 0
    adrs_written: int = 0
    adrs_deleted: int = 0
    embeddings_requested: int = 0


@dataclass(slots=True)
class _ReindexedFile:
    """Bookkeeping for the second-pass call resolver."""

    file_id: UUID
    path: str
    symbols: dict[str, UUID] = field(default_factory=dict)


@dataclass(slots=True)
class _BuilderEntryView:
    """Structural match for :class:`codereview.graph.builder._FileEntry`.

    ``_resolve_call`` reads ``symbols`` and ``imports`` via attribute access,
    so we don't need to instantiate the private ``_FileEntry`` type; any
    dataclass with the same shape works and keeps the import surface small.
    """

    file_id: UUID
    path: str
    symbols: dict[str, UUID]
    imports: list[ImportRef]


class IncrementalUpdater:
    """Apply a :class:`FileChange` batch to an existing repository graph."""

    def __init__(
        self,
        session: AsyncSession,
        repository_id: UUID,
        *,
        embedding_pipeline: EmbeddingPipeline | None = None,
    ) -> None:
        self._session = session
        self._repository_id = repository_id
        self._pipeline = embedding_pipeline
        self._parsers: dict[str, LanguageParser] = {}
        self.stats = IncrementalStats()
        self._reindexed: dict[str, _ReindexedFile] = {}
        # Populated for the duration of a single ``apply`` call so the second
        # pass can reopen files via absolute paths.
        self._repo_root: Path | None = None

    # -- Public API ----------------------------------------------------------

    async def apply(
        self,
        repo_root: Path,
        changes: Iterable[FileChange],
        *,
        head_sha: str,
    ) -> IncrementalStats:
        """Apply ``changes`` to the graph and bump ``last_indexed_sha``."""
        self._repo_root = repo_root
        try:
            for change in changes:
                if _is_adr_path(change.path):
                    if change.kind == "D":
                        await self._remove_adr(change.path)
                    else:
                        await self._reindex_adr(repo_root, change.path)
                    continue

                parser_cls = parser_for_path(change.path)
                if parser_cls is None:
                    # Non-source, non-ADR change (README, CI config, etc.).
                    continue
                if change.kind == "D":
                    await self._remove_file(change.path)
                else:
                    await self._reindex_file(
                        repo_root, change.path, kind=change.kind, head_sha=head_sha
                    )

            await self._resolve_changed_call_edges()

            if self._pipeline is not None:
                await self._pipeline.flush()
            await self._mark_indexed(head_sha)
            await self._session.flush()
            return self.stats
        finally:
            self._repo_root = None

    async def update_from_git(
        self,
        repo_root: Path,
        *,
        base_sha: str,
        head_sha: str,
    ) -> IncrementalStats:
        """Compute changes via ``git diff`` and apply them."""
        changes = await diff_head_vs_base(repo_root, base_sha=base_sha, head_sha=head_sha)
        return await self.apply(repo_root, changes, head_sha=head_sha)

    # -- File removal --------------------------------------------------------

    async def _remove_file(self, path: str) -> None:
        stmt = select(m.File).where(
            m.File.repository_id == self._repository_id, m.File.path == path
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return
        # Count the symbols we are about to nuke via FK cascade so the stats
        # line up with what consumers will see in the graph.
        symbol_count = (
            await self._session.execute(select(m.Symbol.id).where(m.Symbol.file_id == existing.id))
        ).all()
        self.stats.symbols_deleted += len(symbol_count)
        await self._session.delete(existing)
        await self._session.flush()
        self.stats.files_removed += 1

    # -- File add / modify ---------------------------------------------------

    async def _reindex_file(
        self,
        repo_root: Path,
        path: str,
        *,
        kind: ChangeKind,
        head_sha: str,
    ) -> None:
        parser_cls = parser_for_path(path)
        if parser_cls is None:
            return
        parser = self._get_parser(parser_cls)

        fs_path = repo_root / path
        try:
            text = fs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return

        tree = parser.parse(text)
        new_symbols = parser.extract_symbols(tree, path)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        file_row, outcome = await self._upsert_file(
            path=path, content_hash=content_hash, language=parser.name, head_sha=head_sha
        )
        if outcome == "added":
            self.stats.files_added += 1
        elif outcome == "modified":
            self.stats.files_modified += 1
        # "unchanged" is possible when the caller handed us a stale diff entry;
        # we still want to re-resolve outgoing edges in case *other* files in
        # the batch changed, so we fall through to re-sync symbols anyway.

        entry = _ReindexedFile(file_id=file_row.id, path=path)
        await self._sync_symbols(file_row.id, new_symbols, entry)
        self._reindexed[path] = entry

    async def _upsert_file(
        self,
        *,
        path: str,
        content_hash: str,
        language: str,
        head_sha: str,
    ) -> tuple[m.File, Literal["added", "modified", "unchanged"]]:
        """Upsert the file row in place.

        The row's primary key is preserved across content changes so any
        ``Symbol`` rows bound to it survive for the subsequent
        :meth:`_sync_symbols` pass.
        """
        stmt = select(m.File).where(
            m.File.repository_id == self._repository_id, m.File.path == path
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            if existing.content_hash == content_hash:
                existing.last_seen_sha = head_sha
                return existing, "unchanged"
            existing.content_hash = content_hash
            existing.language = language
            existing.last_seen_sha = head_sha
            await self._session.flush()
            return existing, "modified"

        row = m.File(
            id=uuid4(),
            repository_id=self._repository_id,
            path=path,
            language=language,
            content_hash=content_hash,
            first_seen_sha=head_sha,
            last_seen_sha=head_sha,
        )
        self._session.add(row)
        await self._session.flush()
        return row, "added"

    async def _sync_symbols(
        self,
        file_id: UUID,
        new_symbols: Iterable[Symbol],
        entry: _ReindexedFile,
    ) -> None:
        """Upsert + prune symbols for a single file.

        Preserves ``Symbol.id`` when the ``qualified_name`` still exists,
        which keeps incoming ``symbol_calls`` edges pointing at the right row
        even if the body changed.
        """
        existing_rows = (
            (await self._session.execute(select(m.Symbol).where(m.Symbol.file_id == file_id)))
            .scalars()
            .all()
        )
        existing_by_qname: dict[str, m.Symbol] = {s.qualified_name: s for s in existing_rows}

        seen: set[str] = set()
        for sym in new_symbols:
            seen.add(sym.qualified_name)
            row = existing_by_qname.get(sym.qualified_name)
            if row is None:
                row = m.Symbol(
                    id=uuid4(),
                    repository_id=self._repository_id,
                    file_id=file_id,
                    qualified_name=sym.qualified_name,
                    kind=sym.kind,
                    signature=sym.signature,
                    ast_hash=sym.ast_hash,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                )
                self._session.add(row)
                await self._session.flush()
                self.stats.symbols_written += 1
            else:
                row.kind = sym.kind
                row.signature = sym.signature
                row.ast_hash = sym.ast_hash
                row.start_line = sym.start_line
                row.end_line = sym.end_line
                self.stats.symbols_written += 1
            entry.symbols[sym.qualified_name] = row.id
            if self._pipeline is not None:
                await self._pipeline.submit(
                    EmbeddingRequest(
                        kind="symbol",
                        content=_symbol_embed_text(sym),
                        tag=f"symbol:{row.id}",
                    )
                )
                self.stats.embeddings_requested += 1

        # Prune symbols no longer present in the file.
        for qname, row in existing_by_qname.items():
            if qname not in seen:
                await self._session.delete(row)
                self.stats.symbols_deleted += 1
        await self._session.flush()

    # -- Edge recomputation --------------------------------------------------

    async def _resolve_changed_call_edges(self) -> None:
        """Rebuild ``symbol_calls`` for callers that live in reindexed files."""
        if not self._reindexed:
            return

        # Delete outgoing edges from any symbol in a reindexed file. Incoming
        # edges from unchanged files that happened to point at removed
        # symbols were already cascade-deleted by SQLAlchemy when the symbol
        # row vanished.
        caller_file_ids = [entry.file_id for entry in self._reindexed.values()]
        changed_caller_ids = (
            (
                await self._session.execute(
                    select(m.Symbol.id).where(m.Symbol.file_id.in_(caller_file_ids))
                )
            )
            .scalars()
            .all()
        )
        if changed_caller_ids:
            await self._session.execute(
                delete(m.SymbolCall).where(m.SymbolCall.caller_id.in_(changed_caller_ids))
            )
            await self._session.flush()

        # Build a repo-wide qualified-name index: every symbol row currently
        # in the graph, not just the changed ones, so cross-file calls can
        # resolve against untouched files too.
        qname_index: dict[str, UUID] = {}
        all_syms = (
            await self._session.execute(
                select(m.Symbol.qualified_name, m.Symbol.id).where(
                    m.Symbol.repository_id == self._repository_id
                )
            )
        ).all()
        for qname, sym_id in all_syms:
            qname_index.setdefault(qname, sym_id)

        # Re-use the builder's pure resolver helpers so incremental and full
        # rebuilds stay behaviour-equivalent.
        from codereview.graph.builder import _resolve_call

        repo_root = self._repo_root
        for entry in self._reindexed.values():
            parser_cls = parser_for_path(entry.path)
            if parser_cls is None:
                continue
            parser = self._get_parser(parser_cls)
            abs_path = repo_root / entry.path if repo_root is not None else Path(entry.path)
            text = _safe_read(abs_path)
            if text is None:
                continue
            tree = parser.parse(text)
            imports = parser.extract_imports(tree, entry.path)
            calls = parser.extract_calls(tree, entry.path)

            # Wrap the imports onto the _FileEntry shape the builder resolver
            # expects so we can delegate to the shared helper.
            builder_entry = _BuilderEntryView(
                file_id=entry.file_id,
                path=entry.path,
                symbols=entry.symbols,
                imports=list(imports),
            )
            for call in calls:
                caller_id = entry.symbols.get(call.caller_qualified_name)
                if caller_id is None:
                    continue
                if call.dynamic:
                    self.stats.dynamic_calls_dropped += 1
                    continue
                callee_id = _resolve_call(call, builder_entry, qname_index)
                if callee_id is None:
                    self.stats.unresolved_calls_dropped += 1
                    continue
                self._session.add(
                    m.SymbolCall(
                        id=uuid4(),
                        caller_id=caller_id,
                        callee_id=callee_id,
                        line=call.line,
                        dynamic=False,
                    )
                )
                self.stats.call_edges_written += 1
        await self._session.flush()

    # -- ADRs ---------------------------------------------------------------

    async def _remove_adr(self, path: str) -> None:
        """Drop the ADR row for ``path`` if it exists."""
        stmt = select(m.ADR).where(m.ADR.repository_id == self._repository_id, m.ADR.path == path)
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return
        await self._session.delete(existing)
        await self._session.flush()
        self.stats.adrs_deleted += 1

    async def _reindex_adr(self, repo_root: Path, path: str) -> None:
        """Parse the ADR at ``path`` and upsert its row.

        Falls back to a delete if the file is still under an ADR directory
        but no longer parses as an ADR — that covers cases where someone
        drops the ``Status:`` line or converts the file into a plain note.
        """
        fs_path = repo_root / path
        text = _safe_read(fs_path)
        if text is None:
            # File vanished between the diff and the read; treat as delete.
            await self._remove_adr(path)
            return

        parsed = parse_adr(Path(path), text)
        if parsed is None:
            await self._remove_adr(path)
            return

        stmt = select(m.ADR).where(m.ADR.repository_id == self._repository_id, m.ADR.path == path)
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.title = parsed.title
            existing.status = parsed.status
            existing.body = parsed.body
            adr_id = existing.id
        else:
            row = m.ADR(
                id=uuid4(),
                repository_id=self._repository_id,
                path=path,
                title=parsed.title,
                status=parsed.status,
                body=parsed.body,
            )
            self._session.add(row)
            await self._session.flush()
            adr_id = row.id
            self.stats.adrs_written += 1

        if self._pipeline is not None:
            await self._pipeline.submit(
                EmbeddingRequest(
                    kind="prose",
                    content=f"{parsed.title}\n\n{parsed.body}",
                    tag=f"adr:{adr_id}",
                )
            )
            self.stats.embeddings_requested += 1

    # -- Repository bookkeeping ---------------------------------------------

    async def _mark_indexed(self, head_sha: str) -> None:
        repo = (
            await self._session.execute(
                select(m.Repository).where(m.Repository.id == self._repository_id)
            )
        ).scalar_one_or_none()
        if repo is not None:
            repo.last_indexed_sha = head_sha

    # -- Helpers -------------------------------------------------------------

    def _get_parser(self, cls: type[LanguageParser]) -> LanguageParser:
        instance = self._parsers.get(cls.name)
        if instance is None:
            instance = cls()
            self._parsers[cls.name] = instance
        return instance


# -- Module helpers ---------------------------------------------------------


def _is_adr_path(path: str) -> bool:
    """Return ``True`` if ``path`` lives under a recognised ADR directory.

    ADRs are markdown files, so we additionally require a ``.md`` suffix to
    keep stray images or tracking files from being routed through the ADR
    pipeline.
    """
    if not path.endswith(".md"):
        return False
    normalised = path.replace("\\", "/")
    return any(
        normalised == f"{prefix}" or normalised.startswith(f"{prefix}/")
        for prefix in ADR_SEARCH_PATHS
    )


def _symbol_embed_text(sym: Symbol) -> str:
    if sym.signature:
        return f"{sym.kind} {sym.signature}"
    return f"{sym.kind} {sym.qualified_name}"


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


async def diff_head_vs_base(repo_root: Path, *, base_sha: str, head_sha: str) -> list[FileChange]:
    """Shell out to ``git diff --name-status`` and parse the output.

    ``A`` and ``M`` map directly; ``D`` marks deletions; renames (``R``) are
    expanded into a delete + add pair (git emits ``R100\\told\\tnew``).
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_root),
        "diff",
        "--name-status",
        "-z",
        f"{base_sha}..{head_sha}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {stderr.decode('utf-8', errors='replace')}")

    changes: list[FileChange] = []
    # -z output is NUL-separated; rename entries consume two path fields.
    tokens = stdout.decode("utf-8", errors="replace").split("\0")
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if not status:
            continue
        if status[0] == "R":
            # Rename: <old>\0<new>
            old = tokens[i] if i < len(tokens) else ""
            new = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
            if old:
                changes.append(FileChange(path=old, kind="D"))
            if new:
                changes.append(FileChange(path=new, kind="A"))
            continue
        path = tokens[i] if i < len(tokens) else ""
        i += 1
        if not path:
            continue
        kind: ChangeKind
        if status[0] == "A":
            kind = "A"
        elif status[0] == "M":
            kind = "M"
        elif status[0] == "D":
            kind = "D"
        else:
            # Unknown status: treat as modification so we reindex rather
            # than silently dropping a change.
            kind = "M"
        changes.append(FileChange(path=path, kind=kind))
    return changes


__all__ = [
    "ChangeKind",
    "FileChange",
    "IncrementalStats",
    "IncrementalUpdater",
    "diff_head_vs_base",
]
