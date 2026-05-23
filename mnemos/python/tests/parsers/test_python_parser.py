"""Unit tests for :class:`codereview.parsers.python.PythonParser`."""

from __future__ import annotations

from pathlib import Path

import pytest

from codereview.parsers.python import PythonParser
from codereview.parsers.registry import parser_for_path

FIXTURE_ROOT = Path(__file__).resolve().parents[2].parent / "fixtures" / "conflict-repo" / "base"


@pytest.fixture
def parser() -> PythonParser:
    return PythonParser()


# -- Symbol extraction ------------------------------------------------------


def test_extract_top_level_function(parser: PythonParser) -> None:
    tree = parser.parse("def foo(x: int) -> int:\n    return x + 1\n")
    symbols = parser.extract_symbols(tree, "m.py")
    assert len(symbols) == 1
    sym = symbols[0]
    assert sym.qualified_name == "foo"
    assert sym.kind == "function"
    assert sym.signature == "foo(x: int) -> int"
    assert sym.start_line == 1
    assert sym.end_line == 2


def test_class_and_methods_get_qualified_names(parser: PythonParser) -> None:
    source = (
        "class Widget:\n"
        "    def __init__(self, x):\n"
        "        self.x = x\n"
        "\n"
        "    def render(self):\n"
        "        return self.x\n"
    )
    tree = parser.parse(source)
    symbols = parser.extract_symbols(tree, "m.py")
    by_name = {s.qualified_name: s for s in symbols}
    assert set(by_name) == {"Widget", "Widget.__init__", "Widget.render"}
    assert by_name["Widget"].kind == "class"
    assert by_name["Widget.render"].kind == "method"


def test_module_constant_detected(parser: PythonParser) -> None:
    tree = parser.parse("CONSTANT = 42\nlower = 1\n")
    symbols = parser.extract_symbols(tree, "m.py")
    names = {s.qualified_name for s in symbols}
    assert names == {"CONSTANT"}
    const = next(s for s in symbols if s.qualified_name == "CONSTANT")
    assert const.kind == "constant"


def test_decorated_function_unwrapped(parser: PythonParser) -> None:
    tree = parser.parse("import functools\n@functools.lru_cache\ndef cached():\n    return 1\n")
    symbols = parser.extract_symbols(tree, "m.py")
    assert any(s.qualified_name == "cached" and s.kind == "function" for s in symbols)


# -- Imports ----------------------------------------------------------------


def test_plain_and_aliased_import(parser: PythonParser) -> None:
    tree = parser.parse("import os\nimport pathlib as p\n")
    imports = parser.extract_imports(tree, "m.py")
    assert [(i.module, i.alias, i.kind) for i in imports] == [
        ("os", None, "import"),
        ("pathlib", "p", "import"),
    ]


def test_from_import_with_multiple_names(parser: PythonParser) -> None:
    tree = parser.parse("from foo.bar import baz, qux as q\n")
    imports = parser.extract_imports(tree, "m.py")
    assert [(i.module, i.symbol, i.alias) for i in imports] == [
        ("foo.bar", "baz", None),
        ("foo.bar", "qux", "q"),
    ]
    assert all(i.kind == "from" for i in imports)


def test_relative_from_import_preserves_dots(parser: PythonParser) -> None:
    tree = parser.parse("from ..billing.invoice import generate_pdf\n")
    imports = parser.extract_imports(tree, "m.py")
    assert len(imports) == 1
    assert imports[0].module == "..billing.invoice"
    assert imports[0].symbol == "generate_pdf"


# -- Calls ------------------------------------------------------------------


def test_call_extraction_separates_direct_and_method(parser: PythonParser) -> None:
    source = "def entry():\n    helper()\n    widget.render()\ndef helper():\n    pass\n"
    tree = parser.parse(source)
    calls = parser.extract_calls(tree, "m.py")
    assert {(c.caller_qualified_name, c.target_name, c.kind) for c in calls} == {
        ("entry", "helper", "direct"),
        ("entry", "widget.render", "method"),
    }


def test_calls_inside_methods_are_scoped(parser: PythonParser) -> None:
    source = "class Widget:\n    def render(self):\n        self.draw()\n        log('ok')\n"
    tree = parser.parse(source)
    calls = parser.extract_calls(tree, "m.py")
    callers = {c.caller_qualified_name for c in calls}
    assert callers == {"Widget.render"}


def test_dynamic_call_flagged(parser: PythonParser) -> None:
    tree = parser.parse("def f():\n    getattr(obj, 'x')()\n")
    calls = parser.extract_calls(tree, "m.py")
    dynamic = [c for c in calls if c.kind == "dynamic"]
    assert dynamic, "expected the outer getattr(...)() call to be marked dynamic"
    # ``getattr(...)`` itself still resolves as a direct call.
    assert any(c.target_name == "getattr" and c.kind == "direct" for c in calls)


# -- AST hash ---------------------------------------------------------------


def test_ast_hash_ignores_comments_and_docstrings(parser: PythonParser) -> None:
    a = parser.parse(
        'def f(x):\n    """doc"""\n    # comment\n    return x + 1\n'
    ).root_node.children[0]
    b = parser.parse("def f(x):\n    return x + 1\n").root_node.children[0]
    assert parser.canonical_ast_hash(a) == parser.canonical_ast_hash(b)


def test_ast_hash_changes_on_structural_edit(parser: PythonParser) -> None:
    a = parser.parse("def f(x):\n    return x + 1\n").root_node.children[0]
    b = parser.parse("def f(x):\n    return x - 1\n").root_node.children[0]
    assert parser.canonical_ast_hash(a) != parser.canonical_ast_hash(b)


def test_ast_hash_tracks_rename(parser: PythonParser) -> None:
    a = parser.parse("def foo():\n    return 1\n").root_node.children[0]
    b = parser.parse("def bar():\n    return 1\n").root_node.children[0]
    assert parser.canonical_ast_hash(a) != parser.canonical_ast_hash(b)


# -- Fixture integration (conflict-repo/base) -------------------------------


def test_parser_registry_dispatches_by_extension() -> None:
    assert parser_for_path("foo/bar.py") is PythonParser
    assert parser_for_path("foo/bar.rb") is None


def test_fixture_generate_pdf_resolves_from_api_handler(parser: PythonParser) -> None:
    """``download_pdf`` in ``api/invoices.py`` should call ``generate_pdf``."""
    invoices = (FIXTURE_ROOT / "src" / "api" / "invoices.py").read_text()
    tree = parser.parse(invoices)
    calls = parser.extract_calls(tree, "src/api/invoices.py")
    calls_from_download = [c for c in calls if c.caller_qualified_name == "download_pdf"]
    assert any(c.target_name == "generate_pdf" for c in calls_from_download), (
        f"expected generate_pdf among {[(c.caller_qualified_name, c.target_name) for c in calls_from_download]}"
    )


def test_fixture_billing_invoice_exposes_generate_pdf(parser: PythonParser) -> None:
    src = (FIXTURE_ROOT / "src" / "billing" / "invoice.py").read_text()
    tree = parser.parse(src)
    symbols = parser.extract_symbols(tree, "src/billing/invoice.py")
    names = {s.qualified_name for s in symbols}
    assert "generate_pdf" in names
    assert "_render_pdf" in names
    gen = next(s for s in symbols if s.qualified_name == "generate_pdf")
    assert gen.signature is not None and "generate_pdf" in gen.signature
