"""Tests for the prompt loader.

Exercises both the happy paths (load a shipped prompt, render, introspect
``prompt_version``) and every error mode the loader is meant to catch at
load time so typos surface during test runs rather than during an
Anthropic call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from codereview.llm.prompts import (
    LoadedPrompt,
    PromptError,
    available_prompts,
    load_prompt,
)

# -- Happy paths ------------------------------------------------------------


def test_load_semantic_conflict_check_v1() -> None:
    p = load_prompt("semantic_conflict_check", "v1")
    assert isinstance(p, LoadedPrompt)
    assert p.name == "semantic_conflict_check"
    assert p.version == "v1"
    assert p.prompt_version == "semantic_conflict_check.v1"
    assert set(p.variables) == {
        "before_signature",
        "after_signature",
        "caller_qualified_name",
        "caller_file_path",
        "caller_snippet",
    }
    # Schema resolved to the real Pydantic class.
    assert issubclass(p.output_schema, BaseModel)
    assert p.output_schema.__name__ == "SemanticCheckResult"
    assert p.system is not None and "code reviewer" in p.system


def test_load_adr_contradiction_check_v1() -> None:
    p = load_prompt("adr_contradiction_check", "v1")
    assert p.prompt_version == "adr_contradiction_check.v1"
    assert "pr_title" in p.variables
    assert p.output_schema.__name__ == "ADRCheckResult"


def test_render_substitutes_all_variables() -> None:
    p = load_prompt("semantic_conflict_check", "v1")
    rendered = p.render(
        {
            "before_signature": "def f(x: int) -> int",
            "after_signature": "def f(x: int, y: int) -> int",
            "caller_qualified_name": "pkg.mod.caller",
            "caller_file_path": "pkg/mod.py",
            "caller_snippet": "f(1)",
        }
    )
    assert "def f(x: int) -> int" in rendered
    assert "def f(x: int, y: int) -> int" in rendered
    assert "pkg.mod.caller" in rendered
    # Extra variables not in ``variables`` are ignored silently — render
    # only looks up the declared list, which matches the load-time check.


def test_available_prompts_finds_shipped_files() -> None:
    prompts = available_prompts()
    assert ("semantic_conflict_check", "v1") in prompts
    assert ("adr_contradiction_check", "v1") in prompts
    # sorted by filename
    assert prompts == sorted(prompts)


# -- Missing-variable rendering --------------------------------------------


def test_render_missing_variable_raises() -> None:
    p = load_prompt("semantic_conflict_check", "v1")
    with pytest.raises(PromptError, match="missing variables"):
        p.render(
            {
                "before_signature": "def f()",
                "after_signature": "def f(x)",
                # caller_* deliberately omitted
            }
        )


# -- Load-time validation helpers ------------------------------------------


def _write(tmp_path: Path, name: str, version: str, content: str) -> Path:
    """Write a prompt file into ``tmp_path`` and return its path."""

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    path = prompts_dir / f"{name}.{version}.md"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def fake_prompts_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the loader at a temp directory for negative tests."""

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    monkeypatch.setattr("codereview.llm.prompts._PROMPTS_DIR", prompts_dir)
    return prompts_dir


def test_missing_file(fake_prompts_dir: Path) -> None:
    with pytest.raises(PromptError, match="no prompt file"):
        load_prompt("does_not_exist", "v1")


def test_missing_leading_fence(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text("no fence here", encoding="utf-8")
    with pytest.raises(PromptError, match="leading frontmatter fence"):
        load_prompt("bad", "v1")


def test_missing_closing_fence(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\nbody never closes\n", encoding="utf-8"
    )
    with pytest.raises(PromptError, match="closing frontmatter fence"):
        load_prompt("bad", "v1")


def test_empty_body(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "output_schema: codereview.agents.conflict.prompts.SemanticCheckResult\n"
        "variables: []\n"
        "---\n\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="empty prompt body"):
        load_prompt("bad", "v1")


def test_header_line_without_colon(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion v1\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="no colon"):
        load_prompt("bad", "v1")


def test_filename_vs_frontmatter_mismatch(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: wrong\nversion: v1\n"
        "output_schema: codereview.agents.conflict.prompts.SemanticCheckResult\n"
        "variables: [x]\n"
        "---\n${x}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="filename promises"):
        load_prompt("bad", "v1")


def test_missing_output_schema(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "variables: [x]\n"
        "---\n${x}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="missing required field 'output_schema'"):
        load_prompt("bad", "v1")


def test_output_schema_not_dotted(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "output_schema: NoDot\n"
        "variables: [x]\n"
        "---\n${x}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="dotted path"):
        load_prompt("bad", "v1")


def test_output_schema_module_missing(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "output_schema: codereview.nowhere.NoSuch\n"
        "variables: [x]\n"
        "---\n${x}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="cannot import"):
        load_prompt("bad", "v1")


def test_output_schema_attribute_missing(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "output_schema: codereview.agents.conflict.prompts.DoesNotExist\n"
        "variables: [x]\n"
        "---\n${x}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="has no attribute DoesNotExist"):
        load_prompt("bad", "v1")


def test_output_schema_not_basemodel(fake_prompts_dir: Path) -> None:
    # ``Path`` is a class but not a BaseModel subclass.
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "output_schema: pathlib.Path\n"
        "variables: [x]\n"
        "---\n${x}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="not a pydantic BaseModel"):
        load_prompt("bad", "v1")


def test_undeclared_placeholder_rejected(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "output_schema: codereview.agents.conflict.prompts.SemanticCheckResult\n"
        "variables: [x]\n"
        "---\n${x} and ${y}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="undeclared variables"):
        load_prompt("bad", "v1")


def test_unused_declared_variable_rejected(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "bad.v1.md").write_text(
        "---\nname: bad\nversion: v1\n"
        "output_schema: codereview.agents.conflict.prompts.SemanticCheckResult\n"
        "variables: [x, y]\n"
        "---\n${x} only\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="never used in body"):
        load_prompt("bad", "v1")


def test_parse_list_accepts_comma_form(fake_prompts_dir: Path) -> None:
    """``variables: a, b`` (no brackets) is valid shorthand."""
    (fake_prompts_dir / "ok.v1.md").write_text(
        "---\nname: ok\nversion: v1\n"
        "output_schema: codereview.agents.conflict.prompts.SemanticCheckResult\n"
        "variables: a, b\n"
        "---\n${a} ${b}\n",
        encoding="utf-8",
    )
    p = load_prompt("ok", "v1")
    assert p.variables == ("a", "b")
    assert p.render({"a": "1", "b": "2"}) == "1 2\n"


def test_system_field_optional(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "ok.v1.md").write_text(
        "---\nname: ok\nversion: v1\n"
        "output_schema: codereview.agents.conflict.prompts.SemanticCheckResult\n"
        "variables: [a]\n"
        "---\n${a}\n",
        encoding="utf-8",
    )
    assert load_prompt("ok", "v1").system is None


def test_comment_and_blank_lines_in_header(fake_prompts_dir: Path) -> None:
    (fake_prompts_dir / "ok.v1.md").write_text(
        "---\n"
        "# a comment line\n"
        "\n"
        "name: ok\n"
        "version: v1\n"
        "output_schema: codereview.agents.conflict.prompts.SemanticCheckResult\n"
        "variables: [a]\n"
        "---\n"
        "${a}\n",
        encoding="utf-8",
    )
    p = load_prompt("ok", "v1")
    assert p.variables == ("a",)


# -- All shipped prompts load cleanly (guards against future typos) --------


def test_every_shipped_prompt_loads() -> None:
    for name, version in available_prompts():
        load_prompt(name, version)  # must not raise
