"""Unit tests for the AST diff classifier.

Each test exercises one branch of the priority table. When multiple kinds
of change happen simultaneously the classifier returns the highest-priority
label and exposes all observed kinds via ``all_changes``; those cases get
their own tests.
"""

from __future__ import annotations

import pytest

from codereview.agents.conflict import Classification, classify_change

# -- Single-kind changes ----------------------------------------------------


def test_added() -> None:
    result = classify_change(
        None,
        "def hello() -> str:\n    return 'hi'\n",
    )
    assert result.classification is Classification.ADDED
    assert result.all_changes == frozenset({Classification.ADDED})
    assert result.after_signature == "def hello() -> str"


def test_deleted() -> None:
    result = classify_change("def gone():\n    pass\n", None)
    assert result.classification is Classification.DELETED
    assert result.all_changes == frozenset({Classification.DELETED})
    assert result.before_signature == "def gone()"


def test_unchanged() -> None:
    src = "def identity(x: int) -> int:\n    return x\n"
    assert classify_change(src, src).classification is Classification.UNCHANGED


def test_unchanged_across_docstring_edit() -> None:
    """Changing only the docstring is an unchanged classification.

    Rationale: docstring-only edits are noise for an agent that cares about
    behaviour and interface stability.
    """
    before = "def f(x: int) -> int:\n    'old doc'\n    return x\n"
    after = "def f(x: int) -> int:\n    'new doc'\n    return x\n"
    assert classify_change(before, after).classification is Classification.UNCHANGED


def test_signature_change_added_param() -> None:
    before = "def f(x: int) -> int:\n    return x\n"
    after = "def f(x: int, y: int = 0) -> int:\n    return x + y\n"
    r = classify_change(before, after)
    assert r.classification is Classification.SIGNATURE_CHANGE
    assert r.after_signature == "def f(x: int, y: int = 0) -> int"


def test_signature_change_annotation_only() -> None:
    before = "def f(x) -> int:\n    return x\n"
    after = "def f(x: int) -> int:\n    return x\n"
    assert classify_change(before, after).classification is Classification.SIGNATURE_CHANGE


def test_return_type_change_without_signature_change() -> None:
    before = "def f(x: int) -> int:\n    return x\n"
    after = "def f(x: int) -> str:\n    return str(x)\n"
    r = classify_change(before, after)
    # Body also changed, so both are present; the *primary* should be
    # return_type_change because it outranks body_only.
    assert r.classification is Classification.RETURN_TYPE_CHANGE
    assert Classification.BODY_ONLY in r.all_changes


def test_exception_change_add() -> None:
    before = "def f(x):\n    return x\n"
    after = "def f(x):\n    if x < 0:\n        raise ValueError('neg')\n    return x\n"
    r = classify_change(before, after)
    # Body also changed — exception_change outranks body_only.
    assert r.classification is Classification.EXCEPTION_CHANGE
    assert "ValueError" in r.added_exceptions
    assert r.removed_exceptions == frozenset()


def test_exception_change_remove() -> None:
    before = "def f(x):\n    if x < 0: raise ValueError('x')\n    return x\n"
    after = "def f(x):\n    return max(x, 0)\n"
    r = classify_change(before, after)
    assert r.classification is Classification.EXCEPTION_CHANGE
    assert r.removed_exceptions == frozenset({"ValueError"})


def test_exception_change_swap() -> None:
    before = "def f(x):\n    if x < 0: raise ValueError('x')\n"
    after = "def f(x):\n    if x < 0: raise TypeError('x')\n"
    r = classify_change(before, after)
    assert r.classification is Classification.EXCEPTION_CHANGE
    assert r.added_exceptions == frozenset({"TypeError"})
    assert r.removed_exceptions == frozenset({"ValueError"})


def test_exception_with_dotted_path() -> None:
    before = "def f(x):\n    raise billing.exceptions.InvoiceNotFound('x')\n"
    after = "def f(x):\n    raise billing.exceptions.InvoiceAlreadyPaid('x')\n"
    r = classify_change(before, after)
    assert r.classification is Classification.EXCEPTION_CHANGE
    assert r.added_exceptions == frozenset({"billing.exceptions.InvoiceAlreadyPaid"})
    assert r.removed_exceptions == frozenset({"billing.exceptions.InvoiceNotFound"})


def test_bare_reraise_contributes_nothing() -> None:
    before = "def f(x):\n    try: g(x)\n    except: pass\n"
    after = "def f(x):\n    try: g(x)\n    except: raise\n"
    r = classify_change(before, after)
    # The set of exception *classes* raised is unchanged (empty in both).
    assert Classification.EXCEPTION_CHANGE not in r.all_changes
    assert r.classification is Classification.BODY_ONLY


def test_body_only_when_interface_stable() -> None:
    before = "def f(x: int) -> int:\n    return x + 1\n"
    after = "def f(x: int) -> int:\n    return x + 2\n"
    assert classify_change(before, after).classification is Classification.BODY_ONLY


# -- Priority ---------------------------------------------------------------


def test_signature_wins_over_body_and_return_type() -> None:
    """Signature change is the most user-visible; it outranks the rest."""

    before = "def f(x: int) -> int:\n    return x\n"
    after = "def f(x: int, y: int) -> str:\n    return str(x + y)\n"
    r = classify_change(before, after)
    assert r.classification is Classification.SIGNATURE_CHANGE
    # The other kinds are still observable via all_changes.
    assert Classification.RETURN_TYPE_CHANGE in r.all_changes
    assert Classification.BODY_ONLY in r.all_changes


def test_delete_wins_over_any_edit() -> None:
    r = classify_change("def f():\n    pass\n", None)
    assert r.classification is Classification.DELETED


# -- Classes ----------------------------------------------------------------


def test_class_body_change() -> None:
    before = "class A:\n    x = 1\n"
    after = "class A:\n    x = 2\n"
    assert classify_change(before, after).classification is Classification.BODY_ONLY


def test_class_base_change_is_body_only() -> None:
    """Class base-list changes bucket into body_only for v0.1.

    Rationale: distinguishing "inheritance changed" isn't worth its own
    classification yet; downstream agents key on signature/exception/body
    only. Document the decision here so the test is the spec.
    """
    before = "class A(Base1):\n    pass\n"
    after = "class A(Base2):\n    pass\n"
    assert classify_change(before, after).classification is Classification.BODY_ONLY


def test_class_unchanged_body() -> None:
    src = "class A:\n    x = 1\n"
    assert classify_change(src, src).classification is Classification.UNCHANGED


# -- Fixture integration ----------------------------------------------------


def test_fixture_semantic_generate_pdf_is_signature_change() -> None:
    """The conflict-repo semantic branch changes ``generate_pdf``'s signature.

    This test locks the fixture classification to what the Phase 4 plan
    requires: the classifier must label this branch as ``signature_change``
    so the agent fires the caller-compatibility LLM check.
    """
    before = (
        "def generate_pdf(invoice_id: int, repo: InvoiceRepository) -> bytes:\n"
        "    invoice = repo.get_by_id(invoice_id)\n"
        "    if invoice is None:\n"
        "        raise InvoiceNotFound(f'invoice {invoice_id} not found')\n"
        "    return _render_pdf(invoice)\n"
    )
    after = (
        "def generate_pdf(invoice: Invoice) -> bytes:\n"
        "    if invoice is None:\n"
        "        raise InvoiceNotFound('invoice is None')\n"
        "    return _render_pdf(invoice)\n"
    )
    r = classify_change(before, after)
    assert r.classification is Classification.SIGNATURE_CHANGE
    assert r.before_signature == (
        "def generate_pdf(invoice_id: int, repo: InvoiceRepository) -> bytes"
    )
    assert r.after_signature == "def generate_pdf(invoice: Invoice) -> bytes"


# -- Error modes ------------------------------------------------------------


def test_both_none_raises() -> None:
    with pytest.raises(ValueError):
        classify_change(None, None)


def test_multiple_top_level_defs_raises() -> None:
    with pytest.raises(ValueError):
        classify_change("def a(): pass\ndef b(): pass\n", "def a(): pass\n")


def test_no_top_level_def_raises() -> None:
    with pytest.raises(ValueError):
        classify_change("x = 1\n", "def f(): pass\n")


def test_syntax_error_raises_valueerror_not_syntaxerror() -> None:
    """The classifier converts parse errors to ValueError so agents don't
    leak raw :class:`SyntaxError` into review output."""
    with pytest.raises(ValueError):
        classify_change("def f(", "def f(): pass\n")


def test_kind_mismatch_is_signature_change() -> None:
    """Was a function, now a class — definitely not a body-only edit."""
    before = "def Widget():\n    return None\n"
    after = "class Widget:\n    pass\n"
    assert classify_change(before, after).classification is Classification.SIGNATURE_CHANGE
