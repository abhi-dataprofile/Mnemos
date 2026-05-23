"""Architecture Decision Record (ADR) discovery + parsing.

Finds ADR files under a repo and extracts their structured fields. Accepts
both MADR (Markdown Any Decision Record) and Nygard styles: an ADR is any
markdown file under a well-known directory that has a ``Status:`` line and at
least one of a ``## Context`` or ``## Decision`` heading.

We deliberately do not vendor a Markdown parser. ADRs are small; regex-based
extraction is enough for the v0.1 indexer and keeps the dependency graph
small.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Candidate relative paths where ADRs live, in precedence order.
ADR_SEARCH_PATHS: tuple[str, ...] = (
    "docs/adr",
    "docs/architecture",
    "docs/decisions",
    ".adr",
)

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^Status:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_HEADING_CONTEXT_RE = re.compile(r"^##\s+context\b", re.MULTILINE | re.IGNORECASE)
_HEADING_DECISION_RE = re.compile(r"^##\s+decision\b", re.MULTILINE | re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class ParsedADR:
    """One parsed ADR file ready for database upsert."""

    path: str
    title: str
    status: str
    body: str


def discover_adr_files(repo_root: Path) -> list[Path]:
    """Return every candidate ADR file under ``repo_root`` in a stable order."""
    found: list[Path] = []
    for rel in ADR_SEARCH_PATHS:
        base = repo_root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.is_file():
                found.append(path)
    return found


def is_adr(text: str) -> bool:
    """Heuristic: does ``text`` look like an ADR file?

    Require a ``Status:`` line *and* at least one of the Context/Decision
    section headings. Keeps stray markdown files under ``docs/`` from being
    slurped into the ADR table.
    """
    if not _STATUS_RE.search(text):
        return False
    return bool(_HEADING_CONTEXT_RE.search(text) or _HEADING_DECISION_RE.search(text))


def parse_adr(path: Path, text: str) -> ParsedADR | None:
    """Parse the ADR at ``path`` with contents ``text``. Return ``None`` on miss."""
    if not is_adr(text):
        return None

    title_match = _H1_RE.search(text)
    title = title_match.group(1).strip() if title_match else path.stem.replace("-", " ")

    status_match = _STATUS_RE.search(text)
    status = status_match.group(1).strip() if status_match else "proposed"

    # Normalise status to a short token so downstream code can filter on it.
    normalised = status.split()[0].lower() if status else "proposed"

    return ParsedADR(
        path=str(path),
        title=title,
        status=normalised,
        body=text,
    )


def parse_repo_adrs(repo_root: Path) -> list[ParsedADR]:
    """Discover + parse every ADR under ``repo_root``."""
    out: list[ParsedADR] = []
    for candidate in discover_adr_files(repo_root):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_adr(candidate, text)
        if parsed is not None:
            out.append(parsed)
    return out


__all__ = [
    "ADR_SEARCH_PATHS",
    "ParsedADR",
    "discover_adr_files",
    "is_adr",
    "parse_adr",
    "parse_repo_adrs",
]
