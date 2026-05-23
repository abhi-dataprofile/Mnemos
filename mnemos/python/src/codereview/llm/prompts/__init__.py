"""Prompt registry — loads versioned prompt templates from disk.

Prompts live alongside this module as ``<name>.<version>.md`` files. They
have a small frontmatter header followed by the user-prompt body; the
loader parses the header, validates the named output schema, and returns a
:class:`LoadedPrompt` ready to feed to :meth:`LLMClient.structured_call`.

Why versioned files instead of strings in code:
- Diffs of prompt iterations are reviewable in the PR.
- Tests pin a specific version so refactors that drift the prompt blow up
  loudly instead of quietly changing behaviour.
- Prompt authors can edit Markdown without touching Python.

Frontmatter format::

    ---
    name: semantic_conflict_check
    version: v1
    output_schema: codereview.agents.conflict.prompts.SemanticCheckResult
    description: ... single-line summary ...
    variables: [before_signature, after_signature, caller_snippet]
    system: ... one-line system prompt; optional ...
    ---

    Body uses ``${variable}`` substitution. Each name listed in
    ``variables`` must appear at least once and every ``${...}`` in the
    body must resolve to a listed variable. Both invariants are enforced
    by the loader so typos surface at import time, not at LLM call time.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from pydantic import BaseModel

__all__ = [
    "LoadedPrompt",
    "PromptError",
    "available_prompts",
    "load_prompt",
]


_PROMPTS_DIR = Path(__file__).resolve().parent


class PromptError(RuntimeError):
    """Raised when a prompt file is malformed or a render call is missing variables."""


@dataclass(slots=True, frozen=True)
class LoadedPrompt:
    """A parsed prompt ready to render and send to the LLM."""

    name: str
    version: str
    description: str
    variables: tuple[str, ...]
    output_schema: type[BaseModel]
    system: str | None
    _template: Template

    @property
    def prompt_version(self) -> str:
        """Stable identifier for telemetry: ``<name>.<version>``."""
        return f"{self.name}.{self.version}"

    def render(self, variables: dict[str, Any]) -> str:
        """Substitute ``${var}`` placeholders. Missing keys raise."""

        missing = [v for v in self.variables if v not in variables]
        if missing:
            raise PromptError(
                f"prompt {self.prompt_version} missing variables: {', '.join(missing)}"
            )
        # Render with str() coercion so ints/booleans interpolate cleanly.
        ctx = {k: str(variables[k]) for k in self.variables}
        try:
            return self._template.substitute(ctx)
        except KeyError as exc:  # pragma: no cover - guarded by load-time check
            raise PromptError(
                f"prompt {self.prompt_version}: unknown placeholder {exc.args[0]}"
            ) from exc


def load_prompt(name: str, version: str = "v1") -> LoadedPrompt:
    """Load and validate the prompt file at ``<name>.<version>.md``.

    The file is parsed on every call — prompts are tiny, this is fine, and
    it keeps tests honest (no module-level cache to forget about). The
    Anthropic call is the slow part, not file I/O.
    """

    path = _PROMPTS_DIR / f"{name}.{version}.md"
    if not path.is_file():
        raise PromptError(f"no prompt file at {path}")

    raw = path.read_text(encoding="utf-8")
    header, body = _split_frontmatter(raw, source=str(path))
    fields = _parse_header(header, source=str(path))

    declared_name = fields.get("name")
    declared_version = fields.get("version")
    if declared_name != name or declared_version != version:
        raise PromptError(
            f"{path}: filename promises {name}.{version} "
            f"but frontmatter declares {declared_name}.{declared_version}"
        )

    schema_path = fields.get("output_schema")
    if not schema_path:
        raise PromptError(f"{path}: missing required field 'output_schema'")
    schema = _import_schema(schema_path, source=str(path))

    variables = tuple(_parse_list(fields.get("variables", "")))
    template = Template(body)
    _validate_template(template, body, variables, source=str(path))

    return LoadedPrompt(
        name=name,
        version=version,
        description=fields.get("description", ""),
        variables=variables,
        output_schema=schema,
        system=fields.get("system") or None,
        _template=template,
    )


def available_prompts() -> list[tuple[str, str]]:
    """Return ``(name, version)`` for every prompt file on disk.

    Useful for tests that want to assert "every prompt loads cleanly" without
    hard-coding the list.
    """

    out: list[tuple[str, str]] = []
    for p in sorted(_PROMPTS_DIR.glob("*.md")):
        stem = p.stem  # e.g. "semantic_conflict_check.v1"
        if "." not in stem:
            continue
        name, version = stem.rsplit(".", 1)
        out.append((name, version))
    return out


# -- Frontmatter parsing ----------------------------------------------------


def _split_frontmatter(raw: str, *, source: str) -> tuple[str, str]:
    """Return ``(header_text, body_text)``.

    The header is the text between the leading ``---\\n`` and the next
    ``\\n---\\n``. Whitespace inside is preserved so error messages can
    point to the offending line.
    """

    if not raw.startswith("---\n"):
        raise PromptError(f"{source}: missing leading frontmatter fence")
    # Look for the closing fence; allow a trailing newline before it.
    closing = raw.find("\n---\n", 4)
    if closing == -1:
        raise PromptError(f"{source}: missing closing frontmatter fence")
    header = raw[4:closing]
    body = raw[closing + len("\n---\n") :].lstrip("\n")
    if not body.strip():
        raise PromptError(f"{source}: empty prompt body")
    return header, body


def _parse_header(header: str, *, source: str) -> dict[str, str]:
    """Parse a flat ``key: value`` header. Values are single-line strings."""

    fields: dict[str, str] = {}
    for line_no, raw_line in enumerate(header.splitlines(), start=1):
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise PromptError(f"{source}: header line {line_no}: no colon: {line!r}")
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _parse_list(value: str) -> list[str]:
    """Parse ``[a, b, c]`` or ``a, b, c`` into a list of strings."""

    s = value.strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [piece.strip() for piece in s.split(",") if piece.strip()]


def _import_schema(dotted: str, *, source: str) -> type[BaseModel]:
    """Resolve ``module.path.ClassName`` into an actual Pydantic model class."""

    if "." not in dotted:
        raise PromptError(f"{source}: output_schema {dotted!r} must be a dotted path")
    module_path, _, class_name = dotted.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise PromptError(f"{source}: cannot import {module_path}: {exc}") from exc
    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise PromptError(
            f"{source}: module {module_path} has no attribute {class_name}"
        ) from exc
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise PromptError(f"{source}: {dotted} is not a pydantic BaseModel subclass")
    return cls


def _validate_template(
    template: Template,
    body: str,
    declared: tuple[str, ...],
    *,
    source: str,
) -> None:
    """Reject unknown placeholders and unused variables at load time."""

    found = _template_identifiers(template, body)
    declared_set = set(declared)
    unknown = found - declared_set
    if unknown:
        raise PromptError(
            f"{source}: body uses undeclared variables: {', '.join(sorted(unknown))}"
        )
    unused = declared_set - found
    if unused:
        raise PromptError(
            f"{source}: declared variables never used in body: {', '.join(sorted(unused))}"
        )


def _template_identifiers(template: Template, body: str) -> set[str]:
    """Return the set of placeholder names in ``template``.

    Uses :meth:`string.Template.get_identifiers` when available (Python
    3.11+); otherwise walks ``Template.pattern`` manually. We can't just
    grep for ``${name}`` because ``$$`` is an escape and ``$foo`` (no
    braces) is also valid — the pattern already encodes all of that.
    """

    getter = getattr(template, "get_identifiers", None)
    if getter is not None:
        return set(getter())

    # Fallback for Python 3.10: iterate the Template regex directly. The
    # pattern's named groups distinguish escaped / invalid / identifier
    # matches; only the last two actually refer to a variable name.
    found: set[str] = set()
    for match in template.pattern.finditer(body):
        named = match.group("named") or match.group("braced")
        if named is not None:
            found.add(named)
    return found
