"""Unit tests for :mod:`codereview.agents.router.codeowners`."""

from __future__ import annotations

from codereview.agents.router.codeowners import (
    CodeOwnerEntry,
    match_owners,
    parse_codeowners,
)

# -- CodeOwnerEntry.matches ------------------------------------------------


def test_bare_pattern_matches_anywhere_in_tree() -> None:
    e = CodeOwnerEntry(pattern="*.py", owners=("py-team",))
    assert e.matches("foo.py")
    assert e.matches("src/foo.py")
    assert e.matches("src/deep/nest/foo.py")


def test_bare_pattern_does_not_match_other_extensions() -> None:
    e = CodeOwnerEntry(pattern="*.py", owners=("py-team",))
    assert not e.matches("foo.ts")
    assert not e.matches("foo")


def test_directory_glob_matches_nested() -> None:
    e = CodeOwnerEntry(pattern="src/billing/**", owners=("bill",))
    assert e.matches("src/billing/a.py")
    assert e.matches("src/billing/nested/b.py")
    assert not e.matches("src/api/a.py")


def test_trailing_slash_matches_everything_under() -> None:
    e = CodeOwnerEntry(pattern="docs/", owners=("docs-team",))
    assert e.matches("docs/intro.md")
    assert e.matches("docs/guides/a.md")
    assert not e.matches("src/a.py")


def test_leading_slash_anchors_at_root() -> None:
    e = CodeOwnerEntry(pattern="/README.md", owners=("dan",))
    assert e.matches("README.md")
    # A bare pattern would have matched this; the leading slash
    # suppresses that behaviour.
    assert not e.matches("docs/README.md")


def test_single_star_does_not_cross_slash() -> None:
    e = CodeOwnerEntry(pattern="src/*.py", owners=("root",))
    assert e.matches("src/a.py")
    # Single ``*`` must NOT cross a ``/``.
    assert not e.matches("src/nested/a.py")


# -- parse_codeowners ------------------------------------------------------


def test_parse_skips_comments_and_blank_lines() -> None:
    text = """
    # top-of-file comment
    *.py @py-team

    # another
    docs/  @docs-team
    """
    m = parse_codeowners(text)
    # Two real entries landed, ``.py`` and ``docs/``.
    assert len(m) == 2


def test_parse_keeps_multiple_owners_per_pattern() -> None:
    m = parse_codeowners("src/billing/** @billing-team @alice @bob\n")
    assert m.owners_for("src/billing/x.py") == ("billing-team", "alice", "bob")


def test_parse_drops_lines_without_owner_tokens() -> None:
    # No owner tokens → line is effectively a no-op.
    m = parse_codeowners("*.py\n")
    assert m.owners_for("foo.py") == ()


def test_parse_drops_lines_with_email_owners_silently() -> None:
    # We don't support email-style owners; the line has no ``@user``
    # tokens after filtering, so it's dropped entirely.
    m = parse_codeowners("*.py alice@example.com\n")
    assert m.owners_for("foo.py") == ()


def test_parse_strips_leading_at_sign() -> None:
    m = parse_codeowners("*.md @docs-team\n")
    assert m.owners_for("README.md") == ("docs-team",)


def test_parse_keeps_team_handles_intact() -> None:
    m = parse_codeowners("*.ts @acme/frontend\n")
    assert m.owners_for("foo.ts") == ("acme/frontend",)


# -- Last-match-wins -------------------------------------------------------


def test_last_matching_pattern_wins() -> None:
    text = """
    * @fallback
    src/billing/** @billing
    """
    m = parse_codeowners(text)
    # ``src/billing/x.py`` matches both patterns; the later one wins.
    assert m.owners_for("src/billing/x.py") == ("billing",)
    # A path that only matches the first still uses the first.
    assert m.owners_for("src/api/x.py") == ("fallback",)


def test_owners_for_returns_empty_on_no_match() -> None:
    m = parse_codeowners("*.py @py-team\n")
    assert m.owners_for("foo.ts") == ()


# -- match_owners ---------------------------------------------------------


def test_match_owners_unions_across_paths() -> None:
    text = """
    *.py @py-team
    src/billing/** @billing
    """
    m = parse_codeowners(text)
    # py-team matches a.py; billing matches the billing path (which ALSO
    # matches *.py but last-match-wins gives billing). Union is both.
    assert match_owners(m, ["src/other/a.py", "src/billing/b.py"]) == {
        "py-team",
        "billing",
    }


def test_match_owners_empty_paths_returns_empty_set() -> None:
    m = parse_codeowners("*.py @py-team\n")
    assert match_owners(m, []) == set()
