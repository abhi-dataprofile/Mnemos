"""Initial repository indexer.

The builder walks a repository checkout, parses every file the parser
registry recognises, and upserts the resulting symbols, imports, and call
edges into the memory graph. ADRs under the conventional directories are
also discovered and stored.

Scope for v0.1:

- **Working-tree indexing only**. Git history walking, PR ingestion, and
  incremental updates live elsewhere (Phase 2 task 3 / Phase 3). This module
  handles the "given a clone on disk, populate the graph" step so downstream
  agents have something to read.
- **Python only**. Other languages land when their parsers do.
- **Best-effort call resolution**. Calls that do not match an imported name
  or a sibling symbol are emitted as dynamic edges (callee_id unresolved).
  v0.1 stores only *resolved* edges; unresolved ones are dropped and counted.

All database writes go through the :class:`AsyncSession` passed by the
caller. The builder does not own transaction lifecycle — it executes
:meth:`session.flush` but leaves the final ``commit()`` / ``rollback()`` to
the worker orchestration layer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview.graph import models as m
from codereview.graph.adr import parse_repo_adrs
from codereview.graph.embeddings import EmbeddingPipeline, EmbeddingRequest
from codereview.parsers.base import CallRef, ImportRef, LanguageParser, Symbol
from codereview.parsers.python import PythonParser
from codereview.parsers.registry import parser_for_path


@dataclass(slots=True)
class BuildStats:
    files_indexed: int = 0
    symbols_written: int = 0
    call_edges_written: int = 0
    dynamic_calls_dropped: int = 0
    unresolved_calls_dropped: int = 0
    adrs_written: int = 0
    embeddings_requested: int = 0


@dataclass(slots=True)
class _FileEntry:
    file_id: UUID
    path: str
    symbols: dict[str, UUID] = field(default_factory=dict)  # qualified_name -> id
    imports: list[ImportRef] = field(default_factory=list)


class GraphBuilder:
    """Stateful per-run indexer.

    One instance per repository-index job. Call :meth:`index_working_tree` to
    drive a full pass; access :attr:`stats` afterwards for an audit log.
    """

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
        self.stats = BuildStats()
        # Populated during index runs so the resolver can find sibling symbols.
        self._files: dict[str, _FileEntry] = {}  # path -> entry
        # Stashed for the duration of an indexing run so the second pass can
        # reopen files on disk. Reset to None outside ``index_working_tree``.
        self._repo_root: Path | None = None

    # -- Public API ----------------------------------------------------------

    async def index_working_tree(
        self,
        repo_root: Path,
        *,
        head_sha: str = "working-tree",
    ) -> BuildStats:
        """Index every source + ADR file under ``repo_root``.

        ``head_sha`` is the sha stamped on every ``files`` row's
        ``first_seen_sha`` / ``last_seen_sha`` so the schema invariants hold
        even when there's no real commit yet (fixtures).
        """
        self._repo_root = repo_root
        try:
            await self._ingest_sources(repo_root, head_sha=head_sha)
            await self._resolve_call_edges()
            await self._ingest_adrs(repo_root)
            if self._pipeline is not None:
                await self._pipeline.flush()
            await self._session.flush()
            return self.stats
        finally:
            self._repo_root = None

    # -- Source ingestion ----------------------------------------------------

    async def _ingest_sources(self, repo_root: Path, *, head_sha: str) -> None:
        for path in _iter_source_files(repo_root):
            rel = path.relative_to(repo_root).as_posix()
            parser_cls = parser_for_path(rel)
            if parser_cls is None:
                continue
            parser = self._get_parser(parser_cls)
            text = _safe_read(path)
            if text is None:
                continue

            tree = parser.parse(text)
            symbols = parser.extract_symbols(tree, rel)
            imports = parser.extract_imports(tree, rel)

            file_id = await self._upsert_file(
                rel, text=text, language=parser.name, head_sha=head_sha
            )
            entry = _FileEntry(file_id=file_id, path=rel, imports=list(imports))

            for sym in symbols:
                sym_id = await self._upsert_symbol(file_id, sym)
                entry.symbols[sym.qualified_name] = sym_id
                if self._pipeline is not None:
                    await self._pipeline.submit(
                        EmbeddingRequest(
                            kind="symbol",
                            content=_symbol_embed_text(sym),
                            tag=f"symbol:{sym_id}",
                        )
                    )
                    self.stats.embeddings_requested += 1

            self._files[rel] = entry
            self.stats.files_indexed += 1

    async def _upsert_file(
        self,
        path: str,
        *,
        text: str,
        language: str,
        head_sha: str,
    ) -> UUID:
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stmt = select(m.File).where(
            m.File.repository_id == self._repository_id,
            m.File.path == path,
            m.File.content_hash == content_hash,
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.last_seen_sha = head_sha
            return existing.id
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
        return row.id

    async def _upsert_symbol(self, file_id: UUID, sym: Symbol) -> UUID:
        stmt = select(m.Symbol).where(
            m.Symbol.file_id == file_id,
            m.Symbol.qualified_name == sym.qualified_name,
            m.Symbol.ast_hash == sym.ast_hash,
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.signature = sym.signature
            existing.start_line = sym.start_line
            existing.end_line = sym.end_line
            return existing.id
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
        return row.id

    # -- Call resolution -----------------------------------------------------

    async def _resolve_call_edges(self) -> None:
        """Second pass: walk every parsed file, resolve calls, insert edges."""
        # Build a repo-wide qualified-name index so cross-file direct calls
        # can resolve without re-hitting the DB.
        qname_index: dict[str, UUID] = {}
        for entry in self._files.values():
            for qn, sym_id in entry.symbols.items():
                qname_index.setdefault(qn, sym_id)

        for entry in self._files.values():
            parser_cls = parser_for_path(entry.path)
            if parser_cls is None:
                continue
            parser = self._get_parser(parser_cls)
            # Re-parse: tree-sitter trees are cheap, and the upsert path did
            # not retain them. Phase 3 can cache if profiling shows cost.
            fs_path = (self._repo_root / entry.path) if self._repo_root else None
            text = _safe_read(fs_path) if fs_path else None
            if text is None:
                continue
            tree = parser.parse(text)
            calls = parser.extract_calls(tree, entry.path)

            for call in calls:
                callee_id = _resolve_call(call, entry, qname_index)
                caller_id = entry.symbols.get(call.caller_qualified_name)
                if caller_id is None:
                    continue
                if call.dynamic:
                    self.stats.dynamic_calls_dropped += 1
                    continue
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

    async def _ingest_adrs(self, repo_root: Path) -> None:
        for adr in parse_repo_adrs(repo_root):
            rel_path = str(Path(adr.path).relative_to(repo_root))
            stmt = select(m.ADR).where(
                m.ADR.repository_id == self._repository_id,
                m.ADR.path == rel_path,
            )
            existing = (await self._session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                existing.title = adr.title
                existing.status = adr.status
                existing.body = adr.body
                adr_id = existing.id
            else:
                row = m.ADR(
                    id=uuid4(),
                    repository_id=self._repository_id,
                    path=rel_path,
                    title=adr.title,
                    status=adr.status,
                    body=adr.body,
                )
                self._session.add(row)
                await self._session.flush()
                adr_id = row.id
                self.stats.adrs_written += 1

            if self._pipeline is not None:
                await self._pipeline.submit(
                    EmbeddingRequest(
                        kind="prose",
                        content=f"{adr.title}\n\n{adr.body}",
                        tag=f"adr:{adr_id}",
                    )
                )
                self.stats.embeddings_requested += 1

    # -- Helpers -------------------------------------------------------------

    def _get_parser(self, cls: type[LanguageParser]) -> LanguageParser:
        instance = self._parsers.get(cls.name)
        if instance is None:
            instance = cls()
            self._parsers[cls.name] = instance
        return instance


# -- Module helpers ---------------------------------------------------------

# Extensions we walk; the registry filters further so adding a language here
# without a parser is harmless.
_SOURCE_EXTENSIONS: tuple[str, ...] = (".py",)

# Directories we always skip — test fixtures, vendored deps, etc. tend to
# pollute the graph with irrelevant symbols.
_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
)


def _iter_source_files(root: Path) -> list[Path]:
    """Return every candidate source file under ``root`` in a stable order."""
    out: list[Path] = []

    def walk(p: Path) -> None:
        for child in sorted(p.iterdir()):
            if child.is_dir():
                if child.name in _SKIP_DIRS:
                    continue
                walk(child)
            elif child.is_file() and child.suffix in _SOURCE_EXTENSIONS:
                out.append(child)

    walk(root)
    return out


def _safe_read(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _symbol_embed_text(sym: Symbol) -> str:
    """The text we feed to the embedding model for a symbol.

    Kept small on purpose: signature first, then optional docstring summary if
    available via the signature. Symbol-level embeddings are used for call-
    graph-adjacent similarity, not full body retrieval.
    """
    if sym.signature:
        return f"{sym.kind} {sym.signature}"
    return f"{sym.kind} {sym.qualified_name}"


def _resolve_call(
    call: CallRef,
    entry: _FileEntry,
    qname_index: dict[str, UUID],
) -> UUID | None:
    """Best-effort static resolution of a call site.

    Strategy, in order of confidence:

    1. Exact qualified-name match inside the caller's own file.
    2. ``from x import y`` — match the unaliased name against a symbol whose
       qualified name ends with ``.y`` or equals ``y``.
    3. Bare-name match against the repo-wide qualified-name index.

    Returns ``None`` if none of those fire.
    """
    target = call.target_name

    # 1. Sibling symbol in the same file.
    if target in entry.symbols:
        return entry.symbols[target]

    # 2. ``from module import name`` resolution. Walk imports and look for a
    #    match on the unaliased name (or alias) against the global index.
    for imp in entry.imports:
        candidate = _matches_import(imp, target)
        if candidate is None:
            continue
        resolved = _lookup_global(candidate, qname_index)
        if resolved is not None:
            return resolved

    # 3. Bare-name global lookup (last resort for unqualified calls).
    return _lookup_global(target, qname_index)


def _matches_import(imp: ImportRef, target: str) -> str | None:
    """Return the underlying symbol name to look up, or None if no match."""
    if imp.kind == "from":
        alias = imp.alias or imp.symbol
        if alias == target and imp.symbol:
            return imp.symbol
        # Prefix "module.name" match: ``from foo import bar`` + call ``bar.baz``
        if alias and target.startswith(alias + "."):
            return target[len(alias) + 1 :]
    elif imp.kind == "import":
        alias = imp.alias or imp.module.split(".")[-1]
        if target.startswith(alias + "."):
            return target[len(alias) + 1 :]
    return None


def _lookup_global(name: str, index: dict[str, UUID]) -> UUID | None:
    if name in index:
        return index[name]
    # Suffix fallback — handy when a parser emitted a module-qualified name.
    suffix = f".{name}"
    matches = [sym_id for qn, sym_id in index.items() if qn.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return None


def default_parsers() -> list[type[LanguageParser]]:
    """Convenience for callers assembling a bespoke builder."""
    return [PythonParser]


__all__ = [
    "BuildStats",
    "GraphBuilder",
    "default_parsers",
]
