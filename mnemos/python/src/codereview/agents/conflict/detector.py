"""ConflictDetector agent: the first real Mnemos agent.

Three sub-checks run per agent invocation; all three are independent so
one's failure mode does not cancel the others. In order:

1. **Semantic** — for each changed symbol whose change kind is a
   caller-visible one (signature, return type, raised exceptions,
   delete), look up every known caller in the graph and ask the LLM
   whether the caller is still compatible. A negative answer becomes a
   blocking finding.
2. **Architectural** — embed the PR prose, fetch the top-k accepted
   ADRs by similarity, and ask the LLM per ADR whether the PR
   contradicts it. Severity is taken from the LLM output; we never
   block a PR solely on an ADR check.
3. **Convention drift** — pure-Python heuristic from
   :mod:`codereview.agents.conflict.conventions`. No LLM.

Each sub-check is wrapped in ``try/except`` so one LLM flake does not
lose the other findings. When graph or LLM capabilities are missing
(e.g. the context was built without an embedder), the sub-check degrades
gracefully rather than raising.
"""

from __future__ import annotations

import ast
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from codereview.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    ChangedSymbol,
    Finding,
    Location,
)
from codereview.agents.conflict.ast_diff import Classification, classify_change
from codereview.agents.conflict.conventions import (
    ConventionFinding,
    detect_tuple_return_drift,
)
from codereview.agents.conflict.prompts import ADRCheckResult, SemanticCheckResult
from codereview.llm.prompts import load_prompt
from codereview.logging import get_logger

__all__ = ["ConflictDetector"]

_log = get_logger(__name__)

# Prompts are validated at module import — a typo or rename in a .md
# file makes the agent module fail to load, which makes tests fail loud.
_SEMANTIC_PROMPT = load_prompt("semantic_conflict_check", "v1")
_ADR_PROMPT = load_prompt("adr_contradiction_check", "v1")

# Classifications that warrant a semantic (caller-compatibility) check.
# ``body_only`` and ``unchanged`` do not touch the caller surface.
_CALLER_VISIBLE_CHANGES = frozenset(
    {
        Classification.SIGNATURE_CHANGE,
        Classification.RETURN_TYPE_CHANGE,
        Classification.EXCEPTION_CHANGE,
        Classification.DELETED,
    }
)

# Cap on expensive LLM fan-outs so a pathological PR ("refactor 200 callers
# of a hot helper") cannot blow the coordinator token budget.
_MAX_CALLERS_PER_SYMBOL = 10
_MAX_SIMILAR_ADRS = 5

# Convention-enforced module prefixes. Keep this aligned with the
# heuristics in :mod:`codereview.agents.conflict.conventions`.
_CONVENTION_MODULE_PREFIXES = ("src/billing/",)


# -- Small value types ------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _CallerSnippet:
    """Snippet extracted from the workspace for the semantic prompt."""

    qualified_name: str
    file_path: str
    source: str
    line: int | None


# -- Agent ------------------------------------------------------------------


class ConflictDetector(BaseAgent):
    """Surface semantic, architectural, and convention conflicts on a PR."""

    name: ClassVar[str] = "conflict_detector"
    description: ClassVar[str] = (
        "Detects caller-incompatible symbol changes, PRs that contradict "
        "accepted ADRs, and error-handling convention drift."
    )
    version: ClassVar[str] = "0.1.0"

    async def run(self, ctx: AgentContext) -> AgentResult:
        findings: list[Finding] = []
        metadata: dict[str, Any] = {}

        semantic, semantic_meta = await self._check_semantic(ctx)
        findings.extend(semantic)
        metadata["semantic"] = semantic_meta

        architectural, arch_meta = await self._check_architectural(ctx)
        findings.extend(architectural)
        metadata["architectural"] = arch_meta

        convention, conv_meta = self._check_convention(ctx)
        findings.extend(convention)
        metadata["convention"] = conv_meta

        return AgentResult(
            agent_name=self.name,
            findings=_dedup(findings),
            metadata=metadata,
        )

    # -- 1. Semantic -------------------------------------------------------

    async def _check_semantic(
        self, ctx: AgentContext
    ) -> tuple[list[Finding], dict[str, Any]]:
        findings: list[Finding] = []
        meta: dict[str, Any] = {
            "symbols_checked": 0,
            "callers_checked": 0,
            "skipped": 0,
        }

        for sym in ctx.pr.changed_symbols:
            try:
                classification = _classify_symbol(sym)
            except ValueError as exc:
                meta["skipped"] += 1
                _log.warning(
                    "conflict_detector.classify_skipped",
                    qualified_name=sym.qualified_name,
                    error=str(exc),
                )
                continue

            if classification is None or classification not in _CALLER_VISIBLE_CHANGES:
                continue

            meta["symbols_checked"] += 1
            callers = await _callers_of_symbol(ctx, sym)
            for caller in callers[:_MAX_CALLERS_PER_SYMBOL]:
                meta["callers_checked"] += 1
                try:
                    finding = await _semantic_check_call_site(ctx, sym, caller)
                except Exception as exc:
                    _log.warning(
                        "conflict_detector.semantic_error",
                        qualified_name=sym.qualified_name,
                        caller=caller.qualified_name,
                        error=repr(exc),
                    )
                    continue
                if finding is not None:
                    findings.append(finding)

        return findings, meta

    # -- 2. Architectural --------------------------------------------------

    async def _check_architectural(
        self, ctx: AgentContext
    ) -> tuple[list[Finding], dict[str, Any]]:
        findings: list[Finding] = []
        meta: dict[str, Any] = {"adrs_checked": 0, "skipped_reason": None}

        embed_fn = getattr(ctx.llm, "embed_prose", None)
        similar_adrs_fn = getattr(ctx.graph, "similar_adrs", None)
        if embed_fn is None or similar_adrs_fn is None:
            meta["skipped_reason"] = "embed_prose or similar_adrs unavailable"
            return findings, meta

        prose = _pr_prose(ctx)
        try:
            embedding = await embed_fn(prose)
            similar = await similar_adrs_fn(embedding, k=_MAX_SIMILAR_ADRS)
        except Exception as exc:
            meta["skipped_reason"] = f"embedding/similarity failed: {exc!r}"
            _log.warning("conflict_detector.arch_lookup_error", error=repr(exc))
            return findings, meta

        for adr in similar:
            if getattr(adr, "status", None) != "accepted":
                continue
            meta["adrs_checked"] += 1
            try:
                finding = await _adr_check(ctx, adr)
            except Exception as exc:
                _log.warning(
                    "conflict_detector.adr_error",
                    adr=getattr(adr, "title", "?"),
                    error=repr(exc),
                )
                continue
            if finding is not None:
                findings.append(finding)

        return findings, meta

    # -- 3. Convention drift -----------------------------------------------

    def _check_convention(
        self, ctx: AgentContext
    ) -> tuple[list[Finding], dict[str, Any]]:
        findings: list[Finding] = []
        meta: dict[str, Any] = {"files_examined": 0}

        root = ctx.workspace_root
        if root is None:
            meta["skipped_reason"] = "workspace_root not set"
            return findings, meta

        # Which module dirs overlap the PR's changed files?
        changed_paths = {f.path for f in ctx.pr.changed_files}
        involved_dirs: set[str] = set()
        for path in changed_paths:
            for prefix in _CONVENTION_MODULE_PREFIXES:
                if path.startswith(prefix):
                    involved_dirs.add(prefix.rstrip("/"))
        if not involved_dirs:
            return findings, meta

        for mod_dir in involved_dirs:
            module_files = _read_module(root, mod_dir)
            meta["files_examined"] += len(module_files)
            relevant_changed = {p for p in changed_paths if p.startswith(mod_dir + "/")}
            for conv in detect_tuple_return_drift(
                module_files=module_files,
                changed_paths=relevant_changed,
            ):
                findings.append(_convention_to_finding(conv))

        return findings, meta


# -- Helpers ---------------------------------------------------------------


def _classify_symbol(sym: ChangedSymbol) -> Classification | None:
    """Run :func:`classify_change` on the symbol's before/after source.

    Returns ``None`` when the symbol's change kind signals that the
    classifier has nothing to decide (``added`` without old_source,
    ``deleted`` without new_source — both are derivable without the
    classifier via :attr:`ChangedSymbol.change_kind`).
    """

    if sym.change_kind == "added" and sym.new_source is not None:
        return classify_change(None, sym.new_source).classification
    if sym.change_kind == "deleted" and sym.old_source is not None:
        return classify_change(sym.old_source, None).classification
    if sym.old_source is not None and sym.new_source is not None:
        return classify_change(sym.old_source, sym.new_source).classification
    return None


async def _callers_of_symbol(ctx: AgentContext, sym: ChangedSymbol) -> list[Any]:
    """Resolve callers via the graph; tolerate missing methods in tests."""

    sym_by_name = getattr(ctx.graph, "symbol_by_qualified_name", None)
    callers_of = getattr(ctx.graph, "callers_of", None)
    if sym_by_name is None or callers_of is None:
        return []
    try:
        ref = await sym_by_name(ctx.repo_id, sym.qualified_name)
        if ref is None:
            return []
        return list(await callers_of(ref.id))
    except Exception as exc:
        _log.warning(
            "conflict_detector.graph_lookup_error",
            qualified_name=sym.qualified_name,
            error=repr(exc),
        )
        return []


async def _semantic_check_call_site(
    ctx: AgentContext,
    sym: ChangedSymbol,
    caller: Any,
) -> Finding | None:
    """Ask the LLM whether ``caller`` is still compatible with ``sym``."""

    snippet = _extract_caller_snippet(ctx, caller)
    if snippet is None:
        return None

    rendered = _SEMANTIC_PROMPT.render(
        {
            "before_signature": sym.old_signature or "(unknown)",
            "after_signature": sym.new_signature or "(removed)",
            "caller_qualified_name": snippet.qualified_name,
            "caller_file_path": snippet.file_path,
            "caller_snippet": snippet.source,
        }
    )
    result: SemanticCheckResult = await ctx.llm.structured_call(
        prompt=rendered,
        output_schema=SemanticCheckResult,
        prompt_version=_SEMANTIC_PROMPT.prompt_version,
        system=_SEMANTIC_PROMPT.system,
    )
    if result.compatible:
        return None

    title = f"{sym.qualified_name} signature changed; caller may not be updated"
    return Finding(
        severity="blocking",
        kind="semantic",
        title=title,
        detail=result.reason,
        locations=[Location(path=snippet.file_path, line=snippet.line)],
        related_symbols=[sym.qualified_name, snippet.qualified_name],
        suggested_action=result.suggested_fix,
    )


def _extract_caller_snippet(ctx: AgentContext, caller: Any) -> _CallerSnippet | None:
    """Read the caller's function body from ``workspace_root``.

    Falls back to the symbol's signature text if the workspace is not
    available so the semantic check can still run with a minimal
    context (accuracy degrades, but the prompt doesn't crash).
    """

    qualified_name = getattr(caller, "qualified_name", None) or ""
    file_path = getattr(caller, "file_path", None) or ""
    if not qualified_name or not file_path:
        return None

    root = ctx.workspace_root
    if root is not None:
        abs_path = (root / file_path).resolve()
        if _is_within(root, abs_path) and abs_path.is_file():
            source, line = _extract_function_source(abs_path, qualified_name)
            if source is not None:
                return _CallerSnippet(
                    qualified_name=qualified_name,
                    file_path=file_path,
                    source=source,
                    line=line,
                )

    # Degraded fallback: use the signature the graph already has.
    signature = getattr(caller, "signature", None)
    if signature:
        return _CallerSnippet(
            qualified_name=qualified_name,
            file_path=file_path,
            source=f"{signature}\n    ...  # body not available\n",
            line=None,
        )
    return None


def _extract_function_source(path: Path, qualified_name: str) -> tuple[str | None, int | None]:
    """Return the source text + lineno of ``qualified_name`` inside ``path``.

    Qualified names look like ``pkg.module.Class.method`` or
    ``pkg.module.func``. We match on the right-most component because
    the full dotted path is relative to the package, not the file.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return None, None
    short = qualified_name.rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == short:
            return ast.get_source_segment(text, node), node.lineno
    return None, None


def _is_within(root: Path, candidate: Path) -> bool:
    """Defensive check against symlinks / .. escaping the workspace."""

    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return True


async def _adr_check(ctx: AgentContext, adr: Any) -> Finding | None:
    """Ask the LLM whether the PR contradicts ``adr``."""

    rendered = _ADR_PROMPT.render(
        {
            "pr_title": ctx.pr.title,
            "pr_body": ctx.pr.body or "(no description)",
            "diff_summary": _diff_summary(ctx),
            "adr_title": getattr(adr, "title", "(untitled)"),
            "adr_body": getattr(adr, "body", ""),
        }
    )
    result: ADRCheckResult = await ctx.llm.structured_call(
        prompt=rendered,
        output_schema=ADRCheckResult,
        prompt_version=_ADR_PROMPT.prompt_version,
        system=_ADR_PROMPT.system,
    )
    if not result.contradicts:
        return None
    # Point the finding at the first modified file; better targeting
    # would require reading which section of the diff is actually
    # contradictory, and that's Phase 5 work.
    first_path = ctx.pr.changed_files[0].path if ctx.pr.changed_files else ""
    return Finding(
        severity=result.severity,
        kind="architectural",
        title=f"Conflicts with {getattr(adr, 'title', 'an accepted ADR')}",
        detail=result.reasoning,
        locations=[Location(path=first_path)] if first_path else [],
        related_symbols=[],
        suggested_action=(
            f"Either refactor to comply with '{getattr(adr, 'title', 'the ADR')}' "
            "or propose a new ADR that supersedes it."
        ),
    )


def _pr_prose(ctx: AgentContext) -> str:
    return "\n\n".join(
        filter(
            None,
            [ctx.pr.title, ctx.pr.body or "", _diff_summary(ctx)],
        )
    )


def _diff_summary(ctx: AgentContext) -> str:
    """One-line-per-file summary of the diff; cheap and prompt-friendly."""

    lines: list[str] = []
    for f in ctx.pr.changed_files:
        lines.append(f"- {f.change_kind} {f.path}")
    return "\n".join(lines) if lines else "(no changed files recorded)"


def _read_module(root: Path, mod_dir: str) -> dict[str, str]:
    """Return ``{repo-relative path: source}`` for every .py file in ``mod_dir``."""

    base = root / mod_dir
    if not base.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in base.rglob("*.py"):
        # Defense-in-depth against symlinks escaping the workspace.
        with contextlib.suppress(ValueError):
            rel = p.resolve().relative_to(root.resolve()).as_posix()
            with contextlib.suppress(OSError, UnicodeDecodeError):
                out[rel] = p.read_text(encoding="utf-8")
    return out


def _convention_to_finding(conv: ConventionFinding) -> Finding:
    return Finding(
        severity="warning",
        kind="convention",
        title=(
            f"ADR-002 error-handling drift in {conv.file_path}::{conv.function_name}"
        ),
        detail=conv.reason,
        locations=[Location(path=conv.file_path)],
        related_symbols=[conv.function_name],
        suggested_action=conv.suggested_action,
    )


def _dedup(findings: list[Finding]) -> list[Finding]:
    """Collapse duplicates on ``(kind, title, first path)``.

    Duplicate findings can arise when two callers of the same symbol
    both fail the semantic check for the same structural reason; we
    want one entry per logical issue.
    """

    seen: set[tuple[str, str, str]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (
            f.kind,
            f.title,
            f.locations[0].path if f.locations else "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out
