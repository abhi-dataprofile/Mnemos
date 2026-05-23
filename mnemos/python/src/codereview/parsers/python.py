"""Tree-sitter-backed Python parser.

Extracts the signals the indexer needs to populate the memory graph:

- top-level and nested symbols (functions, classes, methods, module constants)
- module-level imports (both ``import x`` and ``from x import y``)
- call sites inside every function body, preserving their line numbers
- a canonical AST hash used to answer "did this symbol really change?"

Dynamic dispatch, monkey-patching, ``getattr``, and deferred imports are out
of scope for v0.1 — see ``docs/adding-a-language.md``. Call sites that cannot
be resolved statically are emitted with ``kind="dynamic"`` so the indexer
can store them at low confidence.
"""

from __future__ import annotations

import hashlib
from typing import ClassVar, cast

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser, Tree

from codereview.parsers.base import (
    CallRef,
    ImportRef,
    LanguageParser,
    Symbol,
    SymbolKind,
)

# Shared language object. ``tree_sitter_python.language()`` returns a PyCapsule
# the ``Language`` ctor wraps; cheap to cache at module scope.
_LANGUAGE = Language(tsp.language())

# Node types that carry identifier-like text we want folded into the AST hash.
# Whitespace-only nodes and comments are elided elsewhere.
_VALUE_NODES: frozenset[str] = frozenset(
    {
        "identifier",
        "integer",
        "float",
        "string_content",
        "true",
        "false",
        "none",
        "ellipsis",
    }
)

# Node types to skip entirely when canonicalising. Comments are the obvious
# case; the docstring detection happens inline (see ``_is_docstring_stmt``).
_SKIP_NODES: frozenset[str] = frozenset({"comment"})


class PythonParser(LanguageParser):
    """tree-sitter-python implementation of :class:`LanguageParser`."""

    name: ClassVar[str] = "python"
    extensions: ClassVar[tuple[str, ...]] = (".py",)

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    # -- Public API ----------------------------------------------------------

    def parse(self, source: str) -> Tree:
        return self._parser.parse(source.encode("utf-8"))

    def extract_symbols(self, tree: Tree, file_path: str) -> list[Symbol]:
        out: list[Symbol] = []
        for child in tree.root_node.children:
            self._collect_symbols(child, [], out)
        return out

    def extract_imports(self, tree: Tree, file_path: str) -> list[ImportRef]:
        out: list[ImportRef] = []
        for child in tree.root_node.children:
            if child.type == "import_statement":
                out.extend(self._parse_plain_import(child, file_path))
            elif child.type == "import_from_statement":
                out.extend(self._parse_from_import(child, file_path))
        return out

    def extract_calls(self, tree: Tree, file_path: str) -> list[CallRef]:
        out: list[CallRef] = []
        for child in tree.root_node.children:
            self._collect_calls(child, [], out)
        return out

    def canonical_ast_hash(self, node: Node) -> str:
        hasher = hashlib.sha256()

        def walk(n: Node, in_body_first: bool) -> None:
            if n.type in _SKIP_NODES:
                return
            if in_body_first and _is_docstring_stmt(n):
                return
            hasher.update(n.type.encode("utf-8"))
            hasher.update(b"\n")
            if n.type in _VALUE_NODES and n.text is not None:
                hasher.update(b"=")
                hasher.update(n.text)
                hasher.update(b"\n")
            if not n.children:
                return
            body = _body_block(n)
            for _idx, child in enumerate(n.children):
                # tree-sitter's Python binding re-wraps nodes on every access
                # so ``is`` comparison fails even for the same underlying node;
                # compare with ``==`` (structural equality).
                is_first_in_body = body is not None and child == body
                if is_first_in_body:
                    # Dive into the block, marking its first statement so the
                    # walker can skip a leading docstring.
                    hasher.update(b"block\n")
                    for j, stmt in enumerate(child.children):
                        walk(stmt, j == 0)
                else:
                    walk(child, False)

        walk(node, False)
        return hasher.hexdigest()

    # -- Symbol collection ---------------------------------------------------

    def _collect_symbols(
        self,
        node: Node,
        scope: list[str],
        out: list[Symbol],
    ) -> None:
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text is None:
                return
            name = name_node.text.decode("utf-8")
            qn = ".".join([*scope, name])
            kind: SymbolKind = "method" if scope else "function"
            out.append(
                Symbol(
                    qualified_name=qn,
                    kind=kind,
                    signature=_function_signature(node),
                    ast_hash=self.canonical_ast_hash(node),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )
            return

        if node.type == "decorated_definition":
            # Unwrap decorator — recurse into the underlying definition.
            target = node.child_by_field_name("definition")
            if target is not None:
                self._collect_symbols(target, scope, out)
            return

        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text is None:
                return
            name = name_node.text.decode("utf-8")
            qn = ".".join([*scope, name])
            out.append(
                Symbol(
                    qualified_name=qn,
                    kind="class",
                    signature=None,
                    ast_hash=self.canonical_ast_hash(node),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )
            body = node.child_by_field_name("body")
            if body is not None:
                for stmt in body.children:
                    self._collect_symbols(stmt, [*scope, name], out)
            return

        if node.type == "expression_statement" and len(node.children) == 1:
            child = node.children[0]
            if child.type == "assignment":
                const = _maybe_constant(child)
                if const is not None:
                    qn = ".".join([*scope, const])
                    out.append(
                        Symbol(
                            qualified_name=qn,
                            kind="constant",
                            signature=None,
                            ast_hash=self.canonical_ast_hash(node),
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                        )
                    )

    # -- Imports -------------------------------------------------------------

    def _parse_plain_import(self, node: Node, file_path: str) -> list[ImportRef]:
        refs: list[ImportRef] = []
        for child in node.children:
            if child.type == "dotted_name" and child.text is not None:
                refs.append(
                    ImportRef(
                        importer_path=file_path,
                        raw=_raw(node),
                        module=child.text.decode("utf-8"),
                        symbol=None,
                        alias=None,
                        kind="import",
                    )
                )
            elif child.type == "aliased_import":
                module_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")
                if (
                    module_node is not None
                    and module_node.text is not None
                    and alias_node is not None
                    and alias_node.text is not None
                ):
                    refs.append(
                        ImportRef(
                            importer_path=file_path,
                            raw=_raw(node),
                            module=module_node.text.decode("utf-8"),
                            symbol=None,
                            alias=alias_node.text.decode("utf-8"),
                            kind="import",
                        )
                    )
        return refs

    def _parse_from_import(self, node: Node, file_path: str) -> list[ImportRef]:
        module_node = node.child_by_field_name("module_name")
        module_text = ""
        if module_node is not None and module_node.text is not None:
            module_text = module_node.text.decode("utf-8")
        imports: list[ImportRef] = []
        # ``name`` fields collect every imported symbol (one per ``dotted_name`` /
        # ``aliased_import`` child that follows the ``import`` keyword).
        for named in _field_children(node, "name"):
            if named.type == "dotted_name" and named.text is not None:
                imports.append(
                    ImportRef(
                        importer_path=file_path,
                        raw=_raw(node),
                        module=module_text,
                        symbol=named.text.decode("utf-8"),
                        alias=None,
                        kind="from",
                    )
                )
            elif named.type == "aliased_import":
                inner_name = named.child_by_field_name("name")
                inner_alias = named.child_by_field_name("alias")
                if (
                    inner_name is not None
                    and inner_name.text is not None
                    and inner_alias is not None
                    and inner_alias.text is not None
                ):
                    imports.append(
                        ImportRef(
                            importer_path=file_path,
                            raw=_raw(node),
                            module=module_text,
                            symbol=inner_name.text.decode("utf-8"),
                            alias=inner_alias.text.decode("utf-8"),
                            kind="from",
                        )
                    )
        # ``from foo import *``: tree-sitter emits a ``wildcard_import`` node.
        for child in node.children:
            if child.type == "wildcard_import":
                imports.append(
                    ImportRef(
                        importer_path=file_path,
                        raw=_raw(node),
                        module=module_text,
                        symbol="*",
                        alias=None,
                        kind="from",
                    )
                )
        return imports

    # -- Calls ---------------------------------------------------------------

    def _collect_calls(
        self,
        node: Node,
        scope: list[str],
        out: list[CallRef],
    ) -> None:
        if node.type == "decorated_definition":
            target = node.child_by_field_name("definition")
            if target is not None:
                self._collect_calls(target, scope, out)
            return

        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text is None:
                return
            name = name_node.text.decode("utf-8")
            body = node.child_by_field_name("body")
            if body is not None:
                for stmt in body.children:
                    self._collect_calls(stmt, [*scope, name], out)
            return

        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text is None:
                return
            name = name_node.text.decode("utf-8")
            caller_qn = ".".join([*scope, name])
            body = node.child_by_field_name("body")
            if body is not None:
                _walk_calls(body, caller_qn, out)
            return

    # No handler for other top-level nodes: module-level calls are ignored on
    # purpose — the graph only models edges between named symbols.


# -- Helpers ----------------------------------------------------------------


def _field_children(node: Node, field_name: str) -> list[Node]:
    """Return every child reachable via ``field_name`` (order-preserving).

    tree-sitter exposes ``child_by_field_name`` (singular) and field ids for
    bulk access. We use the lower-level cursor iteration for portability.
    """
    out: list[Node] = []
    cursor = node.walk()
    if not cursor.goto_first_child():
        return out
    while True:
        if cursor.field_name == field_name:
            out.append(cursor.node)
        if not cursor.goto_next_sibling():
            break
    return out


def _raw(node: Node) -> str:
    text = node.text
    return text.decode("utf-8") if text is not None else ""


def _function_signature(fn: Node) -> str:
    name_node = fn.child_by_field_name("name")
    params = fn.child_by_field_name("parameters")
    ret = fn.child_by_field_name("return_type")
    name_text = name_node.text.decode("utf-8") if name_node and name_node.text else ""
    params_text = params.text.decode("utf-8") if params and params.text else "()"
    if ret is not None and ret.text is not None:
        return f"{name_text}{params_text} -> {ret.text.decode('utf-8')}"
    return f"{name_text}{params_text}"


def _maybe_constant(assignment: Node) -> str | None:
    """Return the identifier name iff ``assignment`` looks like a constant.

    Heuristic: a single ``identifier`` LHS whose text is ``SCREAMING_SNAKE``.
    Annotated constants (``NAME: int = 42``) also count; tree-sitter exposes
    the annotated LHS via a ``left`` field whose type is ``identifier`` in the
    annotation form.
    """
    lhs = assignment.child_by_field_name("left")
    if lhs is None or lhs.text is None:
        return None
    if lhs.type != "identifier":
        return None
    name = lhs.text.decode("utf-8")
    if not name or not name.isupper():
        return None
    # Reject cases like ``A.B = 1`` which should never hit identifier LHS but
    # we belt-and-braces anyway.
    if "." in name:
        return None
    return name


def _is_docstring_stmt(node: Node) -> bool:
    """True when ``node`` is an ``expression_statement`` wrapping a bare string."""
    if node.type != "expression_statement":
        return False
    if not node.children:
        return False
    return node.children[0].type == "string"


def _body_block(node: Node) -> Node | None:
    """Return ``node``'s ``body`` field if it resolves to a ``block``."""
    body = node.child_by_field_name("body")
    if body is None:
        return None
    if body.type != "block":
        return None
    return body


def _walk_calls(node: Node, caller_qn: str, out: list[CallRef]) -> None:
    """DFS through ``node`` collecting ``call`` sites.

    Does not descend into nested ``function_definition`` / ``class_definition``
    — those get their own caller identity when the symbol collector reaches
    them.
    """
    if node.type in ("function_definition", "class_definition"):
        return
    if node.type == "decorated_definition":
        return
    if node.type == "call":
        target = node.child_by_field_name("function")
        if target is not None:
            name, kind, dynamic = _resolve_call_target(target)
            if name is not None:
                out.append(
                    CallRef(
                        caller_qualified_name=caller_qn,
                        target_name=name,
                        line=node.start_point[0] + 1,
                        kind=kind,
                        dynamic=dynamic,
                    )
                )
    for child in node.children:
        _walk_calls(child, caller_qn, out)


def _resolve_call_target(fn: Node) -> tuple[str | None, str, bool]:
    """Classify the callee expression of a ``call`` node.

    Returns ``(name, kind, dynamic)``:
    - ``name`` is the best-effort target identifier / dotted path; ``None``
      when we cannot even guess (complex expressions).
    - ``kind`` is one of ``direct`` / ``method`` / ``dynamic``.
    - ``dynamic`` is ``True`` when the callee is derived at runtime (e.g. a
      call on a call). The indexer stores these at low confidence.
    """
    if fn.type == "identifier" and fn.text is not None:
        return fn.text.decode("utf-8"), "direct", False
    if fn.type == "attribute" and fn.text is not None:
        return fn.text.decode("utf-8"), "method", False
    # ``getattr(x, "y")(...)`` / ``f()()`` / subscript call etc.
    _ = cast(object, fn)  # appease mypy for unused branch
    if fn.text is None:
        return None, "dynamic", True
    return fn.text.decode("utf-8"), "dynamic", True
