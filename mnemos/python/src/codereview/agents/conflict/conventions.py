"""Rule-based convention drift heuristics.

For v0.1 we ship exactly one heuristic: catch billing-module functions
that signal failure via tuple returns when the rest of the module raises
typed exceptions. The conflict-repo ``convention/`` fixture is the
acceptance criterion and the test below pins the behaviour.

Why a heuristic and not an LLM call:

- The signal is structural (return annotation shape + sibling style),
  trivially expressed in AST terms, and the false-positive cost is high
  (would interrupt a PR with a low-information warning). Rule-based
  matching is fast, deterministic, and easy to explain to PR authors.
- The Phase 4 plan explicitly defers other convention checks until they
  earn their own fixture. Each new heuristic should land alongside a
  failing fixture so we don't accumulate untested rules.

The heuristic operates on a snapshot of the module: the caller (the
:class:`ConflictDetector` agent) is responsible for assembling
``module_files`` — typically by combining the PR diff with sibling files
fetched via :class:`GraphClient` or the working tree. Keeping this
module pure-Python lets the test suite exercise it without a database
or network.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ConventionFinding",
    "detect_tuple_return_drift",
]


# -- Public surface ---------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ConventionFinding:
    """One convention-drift observation.

    The agent maps each finding into a :class:`Finding` for the PR
    comment. Keeping a separate dataclass here means the heuristic does
    not depend on the agent framework.
    """

    file_path: str
    function_name: str
    return_annotation_text: str
    reason: str
    suggested_action: str
    related_adrs: tuple[str, ...]


# Modules where the typed-exception convention is enforced for v0.1.
# Keep this list deliberately tiny — every entry is a commitment to a
# fixture and a maintained heuristic.
_BILLING_PREFIX = "src/billing/"
_BILLING_ADR = "docs/adr/adr-002-error-handling.md"


def detect_tuple_return_drift(
    *,
    module_files: Mapping[str, str],
    changed_paths: frozenset[str] | set[str],
) -> list[ConventionFinding]:
    """Flag tuple-return error signalling in ``src/billing/``.

    Parameters
    ----------
    module_files:
        Map of ``file_path -> source_code`` for **all** files currently
        in the module being analysed (the diff *and* its untouched
        siblings). The caller assembles this; the heuristic does no I/O.
    changed_paths:
        Subset of ``module_files`` keys that the PR added or modified.
        The heuristic only flags symbols defined in changed files —
        existing tuple-return code is not the PR author's fault.

    Returns
    -------
    A finding per offending function. Empty when nothing drifts.

    Notes
    -----
    The heuristic fires only when *all* of these hold:

    1. At least one file in ``module_files`` is under ``src/billing/``.
    2. Some file in the module imports from ``.exceptions``
       (the established convention is typed exceptions).
    3. The offending function lives in a changed file under
       ``src/billing/`` and its return annotation matches an
       error-signalling tuple shape (see :func:`_is_error_tuple`).
    4. No *other* (i.e. unchanged, sibling) function in the module
       returns the same shape — drift only matters when the new style
       is new.
    """

    billing_files = {p: s for p, s in module_files.items() if p.startswith(_BILLING_PREFIX)}
    if not billing_files:
        return []

    parsed: dict[str, ast.Module] = {}
    for path, source in billing_files.items():
        try:
            parsed[path] = ast.parse(source, filename=path)
        except SyntaxError:
            # Don't punish an in-progress branch; the parser agent will
            # surface real syntax errors separately.
            continue

    if not _module_imports_exceptions(parsed.values()):
        return []

    # Functions that already use tuple returns in the *unchanged*
    # portion of the module — they're the established style and override
    # the heuristic for this run.
    sibling_tuple_returns = _collect_tuple_return_funcs(
        {p: tree for p, tree in parsed.items() if p not in changed_paths}
    )
    if sibling_tuple_returns:
        return []

    findings: list[ConventionFinding] = []
    for path, tree in parsed.items():
        if path not in changed_paths:
            continue
        for func in _walk_top_level_functions(tree):
            annotation = func.returns
            if annotation is None or not _is_error_tuple(annotation):
                continue
            findings.append(
                ConventionFinding(
                    file_path=path,
                    function_name=func.name,
                    return_annotation_text=ast.unparse(annotation),
                    reason=(
                        f"{path}::{func.name} signals failure via a tuple "
                        f"return ({ast.unparse(annotation)}). The rest of "
                        f"src/billing/ raises BillingError subclasses; "
                        "ADR-002 makes typed exceptions the module "
                        "convention."
                    ),
                    suggested_action=(
                        "Raise a BillingError subclass on failure (or add "
                        "a new one in src/billing/exceptions.py) instead "
                        "of returning a status tuple. See "
                        f"{_BILLING_ADR}."
                    ),
                    related_adrs=(_BILLING_ADR,),
                )
            )
    return findings


# -- Internal helpers -------------------------------------------------------


def _module_imports_exceptions(trees: object) -> bool:
    """Return True if any module imports from a sibling ``.exceptions``.

    Matches both ``from .exceptions import X`` and
    ``from billing.exceptions import X`` so the heuristic survives a
    refactor that changes the import style.
    """

    for tree in trees:  # type: ignore[assignment]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.endswith("exceptions"):
                    return True
    return False


def _collect_tuple_return_funcs(
    parsed: Mapping[str, ast.Module],
) -> list[tuple[str, str]]:
    """Return ``(file, function_name)`` for every tuple-error-returning func."""

    out: list[tuple[str, str]] = []
    for path, tree in parsed.items():
        for func in _walk_top_level_functions(tree):
            if func.returns is not None and _is_error_tuple(func.returns):
                out.append((path, func.name))
    return out


def _walk_top_level_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Iterate top-level ``def`` / ``async def`` (skip dunders and helpers).

    We focus on public functions because private helpers
    (``_already_refunded``) are an implementation detail; the convention
    is about the surface API of the module.
    """

    out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("_"):
                continue
            out.append(node)
    return out


def _is_error_tuple(annotation: ast.expr) -> bool:
    """True if the annotation looks like an error-signalling tuple shape.

    Recognised shapes:

    - ``tuple[bool, ...]``  — (success, error_message_or_more)
    - ``tuple[int, list[str]]`` — (count, errors)
    - ``tuple[int, list[Any]]`` — (count, error-list)

    Anything else (including ``tuple[X, Y, Z]`` with three positional
    elements) is treated as a domain return value, not an error signal.
    """

    args = _tuple_subscript_args(annotation)
    if args is None or len(args) < 2:
        return False

    first = args[0]
    second = args[1]

    if _is_name(first, "bool"):
        return True

    if _is_name(first, "int") and _is_list_subscript(second):
        return True

    return False


def _tuple_subscript_args(node: ast.expr) -> list[ast.expr] | None:
    """Return the type-args of ``tuple[...]`` if ``node`` matches; else None.

    Handles both ``tuple[A, B]`` (PEP 585) and the rarer
    ``typing.Tuple[A, B]`` form by checking the right-most identifier.
    """

    if not isinstance(node, ast.Subscript):
        return None
    base = node.value
    base_name = _trailing_name(base)
    if base_name not in {"tuple", "Tuple"}:
        return None
    slc = node.slice
    if isinstance(slc, ast.Tuple):
        return list(slc.elts)
    return [slc]


def _is_list_subscript(node: ast.expr) -> bool:
    """True for ``list[X]`` / ``typing.List[X]``."""

    if not isinstance(node, ast.Subscript):
        return False
    return _trailing_name(node.value) in {"list", "List"}


def _is_name(node: ast.expr, target: str) -> bool:
    return isinstance(node, ast.Name) and node.id == target


def _trailing_name(node: ast.expr) -> str | None:
    """Return the last identifier in ``node`` for both Name and Attribute."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None
