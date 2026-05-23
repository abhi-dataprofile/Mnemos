"""AST-level change classification for a single Python symbol.

Given the before and after source for one top-level ``def`` / ``async def`` /
``class``, label the change as one of:

- ``added``           — symbol did not exist before
- ``deleted``         — symbol does not exist after
- ``signature_change``— parameter list, defaults, or parameter annotations differ
- ``return_type_change`` — return annotation differs (no other signature change)
- ``exception_change`` — set of raised exception classes differs
- ``body_only``       — internal logic changed, interface stable
- ``unchanged``       — bodies normalize to the same AST

When multiple kinds of change are present simultaneously (e.g. a signature
*and* body change), the classifier returns the most severe label so callers
can key on it without re-analysing. The priority order is:

``deleted > added > signature_change > return_type_change > exception_change > body_only > unchanged``

Why pure stdlib ``ast`` and not the project's tree-sitter parser? The
classifier runs over *single-symbol* source, not whole files. ``ast`` gives
us typed Python nodes out of the box — ``ast.arguments``, ``ast.Raise``,
``ast.FunctionDef`` — with no tree-sitter generics to unpack. The
indexer's tree-sitter path is still the source of truth for call-site
extraction; the two concerns don't overlap.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Classification",
    "ClassificationResult",
    "classify_change",
]


class Classification(str, Enum):  # noqa: UP042 -- StrEnum is 3.11+; 3.10 sandbox still runs tests.
    """Primary label returned by :func:`classify_change`."""

    ADDED = "added"
    DELETED = "deleted"
    SIGNATURE_CHANGE = "signature_change"
    RETURN_TYPE_CHANGE = "return_type_change"
    EXCEPTION_CHANGE = "exception_change"
    BODY_ONLY = "body_only"
    UNCHANGED = "unchanged"


# Higher number = "more important" when multiple things changed. Matches the
# priority described in the module docstring.
_PRIORITY: dict[Classification, int] = {
    Classification.DELETED: 70,
    Classification.ADDED: 60,
    Classification.SIGNATURE_CHANGE: 50,
    Classification.RETURN_TYPE_CHANGE: 40,
    Classification.EXCEPTION_CHANGE: 30,
    Classification.BODY_ONLY: 20,
    Classification.UNCHANGED: 10,
}


@dataclass(slots=True, frozen=True)
class ClassificationResult:
    """Primary label plus every change kind observed.

    ``all_changes`` is useful when more than one dimension changed at once:
    semantic-check prompts want to surface "signature changed AND exceptions
    changed" rather than hiding one behind the other.
    """

    classification: Classification
    all_changes: frozenset[Classification] = field(default_factory=frozenset)
    before_signature: str | None = None
    after_signature: str | None = None
    added_exceptions: frozenset[str] = field(default_factory=frozenset)
    removed_exceptions: frozenset[str] = field(default_factory=frozenset)


# -- Entry point ------------------------------------------------------------


def classify_change(before: str | None, after: str | None) -> ClassificationResult:
    """Classify the change between ``before`` and ``after`` source.

    Either argument may be ``None`` to signal that the symbol does not exist
    on that side (pure add or pure delete). When both are provided they must
    each contain **exactly one** top-level function or class; anything else
    raises :class:`ValueError` so the caller catches programmer error early.

    The function never raises on syntactically valid Python that does not
    match expectations — it degrades to ``UNCHANGED`` rather than crashing a
    whole review.
    """

    if before is None and after is None:
        raise ValueError("classify_change requires at least one of before/after")
    if before is None:
        return ClassificationResult(
            classification=Classification.ADDED,
            all_changes=frozenset({Classification.ADDED}),
            after_signature=_extract_signature_text(_parse_one(after or "")),
        )
    if after is None:
        return ClassificationResult(
            classification=Classification.DELETED,
            all_changes=frozenset({Classification.DELETED}),
            before_signature=_extract_signature_text(_parse_one(before)),
        )

    before_node = _parse_one(before)
    after_node = _parse_one(after)

    changes: set[Classification] = set()
    added_exc: frozenset[str] = frozenset()
    removed_exc: frozenset[str] = frozenset()

    # Functions have signatures + return annotations + exception sets.
    # Classes only compare bodies; we treat a ClassDef diff as body_only.
    if isinstance(before_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
        after_node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        if _args_differ(before_node.args, after_node.args):
            changes.add(Classification.SIGNATURE_CHANGE)
        if _return_annotation_differs(before_node, after_node):
            changes.add(Classification.RETURN_TYPE_CHANGE)
        before_exc = _raised_exception_names(before_node)
        after_exc = _raised_exception_names(after_node)
        if before_exc != after_exc:
            changes.add(Classification.EXCEPTION_CHANGE)
            added_exc = frozenset(after_exc - before_exc)
            removed_exc = frozenset(before_exc - after_exc)

        if _bodies_differ(before_node, after_node):
            changes.add(Classification.BODY_ONLY)

    elif isinstance(before_node, ast.ClassDef) and isinstance(after_node, ast.ClassDef):
        if _class_bodies_differ(before_node, after_node):
            changes.add(Classification.BODY_ONLY)
    else:
        # Kind mismatch (e.g. was a function, now a class). Treat as
        # signature change — the external interface clearly differs.
        changes.add(Classification.SIGNATURE_CHANGE)

    if not changes:
        changes.add(Classification.UNCHANGED)

    primary = max(changes, key=lambda c: _PRIORITY[c])
    return ClassificationResult(
        classification=primary,
        all_changes=frozenset(changes),
        before_signature=_extract_signature_text(before_node),
        after_signature=_extract_signature_text(after_node),
        added_exceptions=added_exc,
        removed_exceptions=removed_exc,
    )


# -- AST helpers ------------------------------------------------------------


def _parse_one(source: str) -> ast.AST:
    """Parse ``source`` and return its single top-level def/class.

    Raises :class:`ValueError` if the source does not contain exactly one
    top-level ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef``.
    """

    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"cannot parse source: {exc}") from exc

    top_defs = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if len(top_defs) != 1:
        raise ValueError(
            f"expected exactly one top-level def/class, found {len(top_defs)}",
        )
    return top_defs[0]


def _args_differ(before: ast.arguments, after: ast.arguments) -> bool:
    """Compare the full argument spec: names, order, defaults, annotations."""

    return _dump_args(before) != _dump_args(after)


def _dump_args(args: ast.arguments) -> str:
    """Canonical string for an :class:`ast.arguments` node.

    ``ast.dump`` on the full node with annotate_fields=False is stable
    enough for diffing. We strip positions/columns via ``include_attributes=False``
    (the default) so relocation inside the file is not a signature change.
    """

    return ast.dump(args, annotate_fields=True)


def _return_annotation_differs(
    before: ast.FunctionDef | ast.AsyncFunctionDef,
    after: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    b = ast.dump(before.returns) if before.returns is not None else ""
    a = ast.dump(after.returns) if after.returns is not None else ""
    return a != b


def _raised_exception_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    """Return the set of exception classes raised anywhere in the function body.

    Handles ``raise Foo(...)``, ``raise Foo``, and ``raise foo.bar.Baz(...)``.
    A bare ``raise`` (re-raise) contributes nothing. Dynamic exception
    factories slip through — the caller's LLM pass catches the residue.
    """

    names: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Raise):
            continue
        exc = sub.exc
        if exc is None:
            continue
        if isinstance(exc, ast.Call):
            target = exc.func
        else:
            target = exc
        name = _attr_chain(target)
        if name is not None:
            names.add(name)
    return frozenset(names)


def _attr_chain(node: ast.AST | None) -> str | None:
    """Collapse ``Attribute``/``Name`` chains into a dotted string."""

    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attr_chain(node.value)
        if base is None:
            return node.attr
        return f"{base}.{node.attr}"
    return None


def _bodies_differ(
    before: ast.FunctionDef | ast.AsyncFunctionDef,
    after: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Compare function bodies ignoring the docstring."""

    return _normalized_body(before) != _normalized_body(after)


def _normalized_body(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> str:
    """Serialize the body minus any leading docstring."""

    stmts = list(node.body)
    if stmts and _is_docstring(stmts[0]):
        stmts = stmts[1:]
    return "\n".join(ast.dump(s, annotate_fields=True) for s in stmts)


def _class_bodies_differ(before: ast.ClassDef, after: ast.ClassDef) -> bool:
    # Bases and keyword args are part of the "signature" of a class; bucket
    # them into body_only here because agents downstream don't distinguish.
    before_key = (
        [ast.dump(b) for b in before.bases],
        [ast.dump(k) for k in before.keywords],
        _normalized_body(before),
    )
    after_key = (
        [ast.dump(b) for b in after.bases],
        [ast.dump(k) for k in after.keywords],
        _normalized_body(after),
    )
    return before_key != after_key


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _extract_signature_text(
    node: ast.AST,
) -> str | None:
    """Human-readable signature string, e.g. ``generate_pdf(invoice: Invoice) -> bytes``.

    Used for the ``before_signature`` / ``after_signature`` fields on the
    result, which the LLM prompt renders back to the model.
    """

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        name = node.name
        params = _format_args(node.args)
        ret = ""
        if node.returns is not None:
            ret = f" -> {ast.unparse(node.returns)}"
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        return f"{prefix}{name}({params}){ret}"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    return None


def _format_args(args: ast.arguments) -> str:
    parts: list[str] = []
    defaults = list(args.defaults)
    pos_args = list(args.posonlyargs) + list(args.args)
    # Align defaults to the *end* of pos_args (Python's rule).
    default_offset = len(pos_args) - len(defaults)
    for i, arg in enumerate(pos_args):
        piece = arg.arg
        if arg.annotation is not None:
            piece += f": {ast.unparse(arg.annotation)}"
        j = i - default_offset
        if j >= 0:
            piece += f" = {ast.unparse(defaults[j])}"
        parts.append(piece)
        if i == len(args.posonlyargs) - 1 and args.posonlyargs:
            parts.append("/")
    if args.vararg is not None:
        piece = f"*{args.vararg.arg}"
        if args.vararg.annotation is not None:
            piece += f": {ast.unparse(args.vararg.annotation)}"
        parts.append(piece)
    elif args.kwonlyargs:
        parts.append("*")
    for kw, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        piece = kw.arg
        if kw.annotation is not None:
            piece += f": {ast.unparse(kw.annotation)}"
        if default is not None:
            piece += f" = {ast.unparse(default)}"
        parts.append(piece)
    if args.kwarg is not None:
        piece = f"**{args.kwarg.arg}"
        if args.kwarg.annotation is not None:
            piece += f": {ast.unparse(args.kwarg.annotation)}"
        parts.append(piece)
    return ", ".join(parts)
