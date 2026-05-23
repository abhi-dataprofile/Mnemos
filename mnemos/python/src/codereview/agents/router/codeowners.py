"""CODEOWNERS parsing for the Reviewer Router.

GitHub's ``CODEOWNERS`` file is the authoritative source of "who owns
this area of the codebase." We want the router to treat a CODEOWNERS
match as a strong positive signal, so Phase 6 ships a real parser
rather than the Phase 2 placeholder (which inferred ownership from
recent-author counts).

Scope for v0.1:

- ``*.py`` / ``src/billing/**`` glob-style patterns (not full gitignore
  syntax — good enough for the common case).
- ``@user`` entries become user logins.
- ``@org/team`` entries are recorded as team handles. We do not resolve
  team membership here; the candidate-pool assembly surfaces team
  handles directly as "codeowner matched" signals without expanding
  them into individual users. Team resolution via the GitHub API is a
  v0.2 feature (documented in the plan doc risks section).
- Comments (``#``) and blank lines are skipped.
- Last matching pattern wins (standard GitHub precedence). A file with
  no matching pattern has no codeowners.

The parser is completely file-system agnostic: callers hand it the
file contents as a string. That keeps this module unit-testable
without touching disk and lets the agent decide where the file came
from (git workspace, repository clone, API fetch).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CODEOWNERS_PATHS",
    "CodeOwnerEntry",
    "CodeOwnersMap",
    "match_owners",
    "parse_codeowners",
]


# Canonical locations GitHub checks, in precedence order. The first one
# that exists wins; this module only parses content, but consumers
# (candidates.py) should probe these paths in order.
CODEOWNERS_PATHS: tuple[str, ...] = (
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
    "CODEOWNERS",
)


@dataclass(slots=True, frozen=True)
class CodeOwnerEntry:
    """One line of ``CODEOWNERS``: a pattern plus its owners."""

    pattern: str
    owners: tuple[str, ...]

    def matches(self, path: str) -> bool:
        """True iff ``path`` matches this entry's pattern.

        Translation rules:

        - A bare pattern like ``*.py`` matches anywhere in the tree
          (GitHub treats these as recursive).
        - A pattern ending in ``/`` matches any file under that
          directory.
        - ``/src/foo`` (leading slash) anchors at the repo root; our
          paths are already relative, so we just strip the leading
          slash and suppress the "match anywhere" rewrite.
        - ``**`` crosses directory separators; a single ``*`` does not.
        """

        pat = self.pattern
        anchored = pat.startswith("/")
        if anchored:
            pat = pat[1:]
        if pat.endswith("/"):
            pat = pat + "**"
        if not anchored and "/" not in pat and not pat.startswith("**"):
            # Bare pattern ⇒ match anywhere in the tree.
            pat = f"**/{pat}"

        regex = _glob_to_regex(pat)
        return regex.fullmatch(path) is not None


class CodeOwnersMap:
    """Parsed CODEOWNERS file. Keeps entries in file order.

    Matches use GitHub's "last matching pattern wins" rule — we iterate
    in reverse and return the first entry whose pattern matches.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: list[CodeOwnerEntry]) -> None:
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._entries)

    def owners_for(self, path: str) -> tuple[str, ...]:
        """Owners for ``path``, or the empty tuple if nothing matches."""

        for entry in reversed(self._entries):
            if entry.matches(path):
                return entry.owners
        return ()


def parse_codeowners(contents: str) -> CodeOwnersMap:
    """Parse the raw text of a ``CODEOWNERS`` file.

    Never raises on malformed input: unparseable lines are silently
    skipped. That matches GitHub's behaviour — malformed ownership
    entries are simply ignored rather than failing the whole file.
    """

    entries: list[CodeOwnerEntry] = []
    for raw in contents.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        tokens = line.split()
        if len(tokens) < 2:
            # Pattern without owners: useless but technically legal;
            # treat as "no owners for this pattern" which effectively
            # drops the line.
            continue

        pattern = tokens[0]
        owners = tuple(_normalise_owner(tok) for tok in tokens[1:] if _is_owner_token(tok))
        if not owners:
            continue
        entries.append(CodeOwnerEntry(pattern=pattern, owners=owners))

    return CodeOwnersMap(entries)


def match_owners(owners_map: CodeOwnersMap, paths: list[str]) -> set[str]:
    """Union of owner handles across ``paths``.

    Returned handles keep their original form (``alice`` for users,
    ``org/team`` for teams). Callers that need to filter out teams can
    do so by checking for ``/`` in the handle.
    """

    out: set[str] = set()
    for path in paths:
        out.update(owners_map.owners_for(path))
    return out


# -- Internals -------------------------------------------------------------


def _is_owner_token(tok: str) -> bool:
    """A CODEOWNERS owner token starts with ``@`` or is an email address.

    We accept ``@user`` and ``@org/team``. Email-style owners (rarely
    used outside self-hosted GitHub Enterprise) are dropped silently —
    the router operates on GitHub logins and has nowhere to file an
    email.
    """

    return tok.startswith("@")


def _normalise_owner(tok: str) -> str:
    """Strip the leading ``@`` so the handle matches ``Person.github_login``."""

    return tok[1:] if tok.startswith("@") else tok


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a CODEOWNERS glob to a regex.

    ``fnmatch`` conflates ``*`` and ``**`` (single ``*`` already
    crosses ``/`` in its dialect), which is the opposite of what we
    want. So the translation is tiny and done by hand:

    - ``**`` matches anything including ``/``
    - ``*``  matches any character except ``/`` (single segment)
    - ``?``  matches any single character except ``/``
    - every other character is regex-escaped literally

    A trailing ``**/`` also absorbs the slash so ``src/**`` matches
    both ``src/foo`` and ``src/foo/bar``. The pattern is anchored with
    ``\\A``/``\\Z`` because CODEOWNERS matches are full-path, not
    substring.
    """

    out: list[str] = [r"\A"]
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*" and i + 1 < n and pattern[i + 1] == "*":
            out.append(".*")
            i += 2
            # ``**/`` — consume the separator; otherwise ``src/**``
            # wouldn't match ``src/foo`` since the trailing slash would
            # require one.
            if i < n and pattern[i] == "/":
                i += 1
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append(r"\Z")
    return re.compile("".join(out))
