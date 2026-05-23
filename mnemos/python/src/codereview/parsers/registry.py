"""Parser registry keyed by file extension.

Phase 1 ships the registry empty. Phase 2 registers ``PythonParser``. New
languages are added here per ``docs/adding-a-language.md``.
"""

from __future__ import annotations

from codereview.parsers.base import LanguageParser
from codereview.parsers.python import PythonParser

PARSERS_BY_EXT: dict[str, type[LanguageParser]] = {
    ext: PythonParser for ext in PythonParser.extensions
}


def parser_for_path(path: str) -> type[LanguageParser] | None:
    """Look up the parser class registered for ``path``'s extension.

    Files with no registered parser are indexed as ``files`` rows with
    ``language=None`` but produce no symbols or edges.
    """

    for ext, parser in PARSERS_BY_EXT.items():
        if path.endswith(ext):
            return parser
    return None
