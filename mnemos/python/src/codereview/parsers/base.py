"""Language parser contract.

Every language Mnemos indexes is represented by a subclass of
:class:`LanguageParser`. See ``docs/adding-a-language.md`` for a narrative
walkthrough.

Phase 1 ships the ABC and its DTOs. A Python implementation lands in Phase 2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

SymbolKind = Literal["function", "class", "method", "constant"]
CallKind = Literal["direct", "method", "dynamic"]


@dataclass(slots=True, frozen=True)
class Symbol:
    """Parser output: a named entity defined in a file."""

    qualified_name: str
    kind: SymbolKind
    signature: str | None
    ast_hash: str
    start_line: int
    end_line: int


@dataclass(slots=True, frozen=True)
class CallRef:
    """Parser output: an unresolved call site inside a symbol body.

    The indexer resolves ``target_name`` against imports to produce a real
    ``symbol_calls`` edge.
    """

    caller_qualified_name: str
    target_name: str
    line: int
    kind: CallKind = "direct"
    dynamic: bool = False


@dataclass(slots=True, frozen=True)
class ImportRef:
    """Parser output: a module-level import statement."""

    importer_path: str
    raw: str
    module: str
    symbol: str | None = None
    alias: str | None = None
    kind: str = "import"
    lazy: bool = False


class LanguageParser(ABC):
    """Abstract base for every language parser."""

    name: ClassVar[str]
    extensions: ClassVar[tuple[str, ...]]

    @abstractmethod
    def parse(self, source: str) -> Any:
        """Return a tree-sitter ``Tree`` (or equivalent) for ``source``."""

    @abstractmethod
    def extract_symbols(self, tree: Any, file_path: str) -> list[Symbol]:
        """Return every symbol defined in ``tree``."""

    @abstractmethod
    def extract_calls(self, tree: Any, file_path: str) -> list[CallRef]:
        """Return every call site inside every function body in ``tree``."""

    @abstractmethod
    def extract_imports(self, tree: Any, file_path: str) -> list[ImportRef]:
        """Return every module-level import statement in ``tree``."""

    @abstractmethod
    def canonical_ast_hash(self, node: Any) -> str:
        """Return a SHA-256 hex digest stable under whitespace + comments."""
