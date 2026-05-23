"""Tests for the rule-based convention drift heuristic.

The fixture integration test is the primary acceptance gate: it locks
the conflict-repo convention branch to a concrete finding so prompt
refactors or AST tweaks cannot silently drop this heuristic's output.
"""

from __future__ import annotations

from pathlib import Path

from codereview.agents.conflict import (
    ConventionFinding,
    detect_tuple_return_drift,
)

# -- Repo paths for fixture integration ------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BASE = _REPO_ROOT / "fixtures" / "conflict-repo" / "base"
_CONVENTION = _REPO_ROOT / "fixtures" / "conflict-repo" / "convention"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _billing_base_files() -> dict[str, str]:
    """Read base-branch billing files keyed by repo-relative path."""

    out: dict[str, str] = {}
    for p in (_BASE / "src" / "billing").rglob("*.py"):
        rel = p.relative_to(_BASE).as_posix()
        out[rel] = _read(p)
    return out


# -- Fixture integration ---------------------------------------------------


def test_fixture_convention_branch_flags_refunds() -> None:
    """convention/src/billing/refunds.py is the signal case for ADR-002.

    The expected/convention.json fixture requires a finding that
    references src/billing/refunds.py and adr-002-error-handling.md.
    """

    module_files = _billing_base_files()
    # Add the new file from the convention branch.
    new_path = "src/billing/refunds.py"
    module_files[new_path] = _read(_CONVENTION / new_path)

    findings = detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={new_path},
    )
    assert len(findings) == 2  # issue_refund + refund_all_for_customer
    names = {f.function_name for f in findings}
    assert names == {"issue_refund", "refund_all_for_customer"}
    for f in findings:
        assert f.file_path == new_path
        assert "docs/adr/adr-002-error-handling.md" in f.related_adrs
        assert "BillingError" in f.suggested_action
        assert "exception" in f.suggested_action.lower()


def test_fixture_base_branch_produces_no_findings() -> None:
    """The base branch follows the typed-exception convention."""

    module_files = _billing_base_files()
    assert detect_tuple_return_drift(
        module_files=module_files,
        changed_paths=set(module_files),  # pretend everything changed
    ) == []


# -- Unit behaviour --------------------------------------------------------


def test_empty_when_no_billing_files() -> None:
    assert detect_tuple_return_drift(
        module_files={"src/other/x.py": "def f() -> int:\n    return 1\n"},
        changed_paths={"src/other/x.py"},
    ) == []


def test_empty_when_module_has_no_exceptions_import() -> None:
    """Heuristic requires an existing typed-exception convention.

    Without ``from .exceptions import X`` somewhere, we have no basis
    to claim tuple returns are a drift.
    """

    module_files = {
        "src/billing/__init__.py": "",
        "src/billing/refunds.py": (
            "def issue_refund() -> tuple[bool, str | None]:\n"
            "    return False, 'oops'\n"
        ),
    }
    assert detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py"},
    ) == []


def test_empty_when_siblings_already_use_tuple_returns() -> None:
    """If the module's existing style is tuple returns, the new file
    is consistent rather than drifting.
    """

    module_files = {
        "src/billing/__init__.py": "",
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/existing.py": (
            "from .exceptions import BillingError\n"
            "def already_tuple() -> tuple[bool, str | None]:\n"
            "    return True, None\n"
        ),
        "src/billing/refunds.py": (
            "from .exceptions import BillingError\n"
            "def issue_refund() -> tuple[bool, str | None]:\n"
            "    return False, 'oops'\n"
        ),
    }
    assert detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py"},
    ) == []


def test_ignores_unchanged_files() -> None:
    """Tuple returns in pre-existing code are not the PR author's fault."""

    module_files = {
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/invoice.py": (
            "from .exceptions import BillingError\n"
            "def generate_pdf() -> bytes:\n    return b''\n"
        ),
        "src/billing/legacy.py": (
            "def legacy() -> tuple[bool, str | None]:\n"
            "    return False, 'oops'\n"
        ),
    }
    # legacy.py wasn't changed; its tuple return counts as sibling
    # convention and suppresses the heuristic entirely.
    assert detect_tuple_return_drift(
        module_files=module_files,
        changed_paths=set(),
    ) == []


def test_skips_private_helpers() -> None:
    """Helpers named ``_foo`` are implementation detail; not on the surface."""

    module_files = {
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/refunds.py": (
            "from .exceptions import BillingError\n"
            "def _private() -> tuple[bool, str | None]:\n"
            "    return False, 'x'\n"
        ),
    }
    assert detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py"},
    ) == []


def test_detects_int_list_tuple_shape() -> None:
    """(count, errors) is a second recognised error-tuple shape."""

    module_files = {
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/refunds.py": (
            "from .exceptions import BillingError\n"
            "def refund_all() -> tuple[int, list[str]]:\n"
            "    return 0, []\n"
        ),
    }
    findings = detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py"},
    )
    assert len(findings) == 1
    assert findings[0].function_name == "refund_all"
    assert "tuple[int, list[str]]" in findings[0].return_annotation_text


def test_ignores_unrelated_tuple_returns() -> None:
    """``tuple[int, int]`` is a domain value (e.g. a range), not an error."""

    module_files = {
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/refunds.py": (
            "from .exceptions import BillingError\n"
            "def window() -> tuple[int, int]:\n"
            "    return 0, 10\n"
        ),
    }
    assert detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py"},
    ) == []


def test_ignores_functions_without_annotations() -> None:
    """The heuristic needs a return annotation to judge."""

    module_files = {
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/refunds.py": (
            "from .exceptions import BillingError\n"
            "def no_hint():\n    return False, 'oops'\n"
        ),
    }
    assert detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py"},
    ) == []


def test_async_functions_are_also_checked() -> None:
    module_files = {
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/refunds.py": (
            "from .exceptions import BillingError\n"
            "async def issue_refund() -> tuple[bool, str | None]:\n"
            "    return False, 'oops'\n"
        ),
    }
    findings = detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py"},
    )
    assert [f.function_name for f in findings] == ["issue_refund"]


def test_syntax_error_in_file_is_tolerated() -> None:
    """A malformed file should not crash the heuristic; the parser
    agent will surface the syntax error separately."""

    module_files = {
        "src/billing/exceptions.py": "class BillingError(Exception): ...\n",
        "src/billing/broken.py": "def f(:\n    pass\n",
        "src/billing/refunds.py": (
            "from .exceptions import BillingError\n"
            "def issue_refund() -> tuple[bool, str | None]:\n"
            "    return False, 'x'\n"
        ),
    }
    findings = detect_tuple_return_drift(
        module_files=module_files,
        changed_paths={"src/billing/refunds.py", "src/billing/broken.py"},
    )
    assert [f.function_name for f in findings] == ["issue_refund"]


def test_finding_dataclass_is_frozen() -> None:
    """ConventionFinding should be hashable and immutable — it shows up
    in sets and dicts when dedup-ing findings."""

    f = ConventionFinding(
        file_path="src/billing/refunds.py",
        function_name="issue_refund",
        return_annotation_text="tuple[bool, str | None]",
        reason="r",
        suggested_action="a",
        related_adrs=("docs/adr/adr-002-error-handling.md",),
    )
    # set() requires hashable; frozen=True + slots=True gives us that.
    assert {f, f} == {f}
