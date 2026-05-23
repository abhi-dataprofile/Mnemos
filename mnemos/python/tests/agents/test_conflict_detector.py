"""ConflictDetector agent tests with fake graph and LLM.

These exercise the three sub-checks in isolation plus their composition.
The fixture-suite integration test (Phase 4 task #46) lives in a
separate module guarded by a real Postgres; this file sticks to
in-memory fakes so it runs in every CI invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from codereview.agents.base import (
    AgentContext,
    ChangedFile,
    ChangedSymbol,
    PullRequestSnapshot,
)
from codereview.agents.conflict import ConflictDetector
from codereview.agents.conflict.prompts import ADRCheckResult, SemanticCheckResult

# -- Fakes -----------------------------------------------------------------


@dataclass
class _FakeSymbolRef:
    id: UUID
    qualified_name: str
    kind: str = "function"
    signature: str | None = None
    file_path: str = ""


@dataclass
class _FakeADR:
    id: UUID
    title: str
    status: str
    body: str


class _FakeGraph:
    def __init__(
        self,
        *,
        callers: dict[str, list[_FakeSymbolRef]] | None = None,
        adrs: list[_FakeADR] | None = None,
    ) -> None:
        self._callers = callers or {}
        self._adrs = adrs or []

    async def symbol_by_qualified_name(
        self, _repo_id: UUID, name: str
    ) -> _FakeSymbolRef | None:
        if name in self._callers:
            return _FakeSymbolRef(id=uuid4(), qualified_name=name)
        return None

    async def callers_of(self, _symbol_id: UUID) -> list[_FakeSymbolRef]:
        # The fake indexes callers by the target symbol's qualified_name,
        # but ``callers_of`` takes an id — return the first non-empty
        # bucket; tests only set one symbol at a time.
        for refs in self._callers.values():
            return refs
        return []

    async def similar_adrs(
        self, _embedding: list[float], *, k: int = 5
    ) -> list[_FakeADR]:
        return self._adrs[:k]


class _FakeLLM:
    """Records every structured_call invocation and returns queued responses.

    Queues are keyed by schema class so tests can set up semantic and
    ADR responses independently.
    """

    def __init__(self) -> None:
        self._responses: dict[type, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.embed_calls: list[str] = []

    def queue(self, schema: type, value: Any) -> None:
        self._responses.setdefault(schema, []).append(value)

    async def embed_prose(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return [0.1, 0.2, 0.3]

    async def structured_call(
        self,
        *,
        prompt: str,
        output_schema: type,
        prompt_version: str,
        system: str | None = None,
        max_tokens: int = 4000,
    ) -> Any:
        self.calls.append(
            {
                "prompt": prompt,
                "schema": output_schema,
                "prompt_version": prompt_version,
                "system": system,
                "max_tokens": max_tokens,
            }
        )
        queue = self._responses.get(output_schema)
        if not queue:
            raise AssertionError(
                f"No queued response for schema={output_schema.__name__}"
            )
        return queue.pop(0)


def _minimal_pr(**overrides: Any) -> PullRequestSnapshot:
    base: dict[str, Any] = {
        "number": 1,
        "title": "Refactor invoice API",
        "body": "Pulls Invoice loading out of generate_pdf",
        "author": "abhi",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
    }
    base.update(overrides)
    return PullRequestSnapshot(**base)


def _ctx(
    *,
    pr: PullRequestSnapshot,
    graph: Any = None,
    llm: Any = None,
    workspace_root: Path | None = None,
) -> AgentContext:
    return AgentContext(
        pr=pr,
        repo_id=uuid4(),
        graph=graph if graph is not None else _FakeGraph(),
        llm=llm if llm is not None else _FakeLLM(),
        workspace_root=workspace_root,
    )


# -- Semantic check --------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_flags_incompatible_caller() -> None:
    """A signature change + an LLM 'not compatible' verdict → blocking finding."""

    sym = ChangedSymbol(
        qualified_name="billing.invoice.generate_pdf",
        kind="function",
        change_kind="modified",
        old_signature="def generate_pdf(invoice_id: int, repo) -> bytes",
        new_signature="def generate_pdf(invoice: Invoice) -> bytes",
        old_source=(
            "def generate_pdf(invoice_id: int, repo) -> bytes:\n"
            "    return b''\n"
        ),
        new_source=(
            "def generate_pdf(invoice: Invoice) -> bytes:\n"
            "    return b''\n"
        ),
        file_path="src/billing/invoice.py",
    )
    pr = _minimal_pr(
        changed_symbols=[sym],
        changed_files=[ChangedFile(path="src/billing/invoice.py", change_kind="modified")],
    )
    graph = _FakeGraph(
        callers={
            "billing.invoice.generate_pdf": [
                _FakeSymbolRef(
                    id=uuid4(),
                    qualified_name="api.customers.render",
                    signature="def render(customer_id: int) -> bytes",
                    file_path="src/api/customers.py",
                )
            ]
        }
    )
    llm = _FakeLLM()
    llm.queue(
        SemanticCheckResult,
        SemanticCheckResult(
            compatible=False,
            reason="render() still passes invoice_id; new signature wants an Invoice.",
            suggested_fix="Load the invoice via repo.get_by_id(...) and pass it.",
        ),
    )

    agent = ConflictDetector()
    result = await agent.run(_ctx(pr=pr, graph=graph, llm=llm))

    semantic = [f for f in result.findings if f.kind == "semantic"]
    assert len(semantic) == 1
    f = semantic[0]
    assert f.severity == "blocking"
    assert "signature changed" in f.title
    assert "invoice_id" in f.detail
    assert f.suggested_action is not None
    assert "Load the invoice" in f.suggested_action
    # Caller file path surfaces on the finding.
    assert f.locations[0].path == "src/api/customers.py"
    # Prompt wiring: the v1 prompt was used.
    semantic_calls = [c for c in llm.calls if c["schema"] is SemanticCheckResult]
    assert len(semantic_calls) == 1
    assert semantic_calls[0]["prompt_version"] == "semantic_conflict_check.v1"


@pytest.mark.asyncio
async def test_semantic_swallows_compatible_responses() -> None:
    sym = ChangedSymbol(
        qualified_name="m.f",
        kind="function",
        change_kind="modified",
        old_signature="def f(x: int) -> int",
        new_signature="def f(x: int, y: int = 0) -> int",
        old_source="def f(x: int) -> int:\n    return x\n",
        new_source="def f(x: int, y: int = 0) -> int:\n    return x + y\n",
        file_path="src/m.py",
    )
    graph = _FakeGraph(
        callers={
            "m.f": [
                _FakeSymbolRef(
                    id=uuid4(),
                    qualified_name="m.caller",
                    signature="def caller() -> int",
                    file_path="src/m.py",
                )
            ]
        }
    )
    llm = _FakeLLM()
    llm.queue(
        SemanticCheckResult,
        SemanticCheckResult(compatible=True, reason="default param keeps caller valid"),
    )

    result = await ConflictDetector().run(
        _ctx(pr=_minimal_pr(changed_symbols=[sym]), graph=graph, llm=llm)
    )
    assert [f for f in result.findings if f.kind == "semantic"] == []


@pytest.mark.asyncio
async def test_semantic_skips_body_only_changes() -> None:
    """body_only doesn't reach the LLM — no tokens burned on safe edits."""

    sym = ChangedSymbol(
        qualified_name="m.f",
        kind="function",
        change_kind="body_only",
        old_signature="def f(x: int) -> int",
        new_signature="def f(x: int) -> int",
        old_source="def f(x: int) -> int:\n    return x + 1\n",
        new_source="def f(x: int) -> int:\n    return x + 2\n",
        file_path="src/m.py",
    )
    llm = _FakeLLM()  # no responses queued — would raise if called
    await ConflictDetector().run(
        _ctx(pr=_minimal_pr(changed_symbols=[sym]), llm=llm)
    )
    assert [c for c in llm.calls if c["schema"] is SemanticCheckResult] == []


@pytest.mark.asyncio
async def test_semantic_skips_when_no_source_available() -> None:
    """Missing before/after source → classifier can't run; the agent
    degrades gracefully instead of exploding."""

    sym = ChangedSymbol(
        qualified_name="m.f",
        kind="function",
        change_kind="modified",
        old_signature="def f(x)",
        new_signature="def f(x, y)",
        file_path="src/m.py",
    )
    llm = _FakeLLM()
    result = await ConflictDetector().run(
        _ctx(pr=_minimal_pr(changed_symbols=[sym]), llm=llm)
    )
    assert result.metadata["semantic"]["symbols_checked"] == 0
    assert [f for f in result.findings if f.kind == "semantic"] == []


@pytest.mark.asyncio
async def test_semantic_error_isolated_across_callers() -> None:
    """One LLM failure must not lose the other caller's finding."""

    sym = ChangedSymbol(
        qualified_name="m.f",
        kind="function",
        change_kind="modified",
        old_signature="def f(x: int) -> int",
        new_signature="def f(x: str) -> int",
        old_source="def f(x: int) -> int:\n    return x\n",
        new_source="def f(x: str) -> int:\n    return int(x)\n",
        file_path="src/m.py",
    )
    graph = _FakeGraph(
        callers={
            "m.f": [
                _FakeSymbolRef(
                    id=uuid4(),
                    qualified_name="m.caller_a",
                    signature="def caller_a() -> int",
                    file_path="src/m.py",
                ),
                _FakeSymbolRef(
                    id=uuid4(),
                    qualified_name="m.caller_b",
                    signature="def caller_b() -> int",
                    file_path="src/m.py",
                ),
            ]
        }
    )

    class _FlakyLLM(_FakeLLM):
        calls_made = 0

        async def structured_call(self, **kwargs: Any) -> Any:  # type: ignore[override]
            _FlakyLLM.calls_made += 1
            if _FlakyLLM.calls_made == 1:
                raise RuntimeError("flaky api")
            return SemanticCheckResult(
                compatible=False,
                reason="caller_b still passes an int",
                suggested_fix="pass str(x)",
            )

    llm = _FlakyLLM()
    result = await ConflictDetector().run(_ctx(pr=_minimal_pr(changed_symbols=[sym]), graph=graph, llm=llm))
    semantic = [f for f in result.findings if f.kind == "semantic"]
    assert len(semantic) == 1  # caller_b's finding survived
    assert any("caller_b" in s for s in semantic[0].related_symbols)


# -- Architectural check ---------------------------------------------------


@pytest.mark.asyncio
async def test_architectural_flags_accepted_contradiction() -> None:
    llm = _FakeLLM()
    llm.queue(
        ADRCheckResult,
        ADRCheckResult(
            contradicts=True,
            reasoning="PR bypasses InvoiceRepository; ADR-001 requires going through it.",
            severity="warning",
        ),
    )
    graph = _FakeGraph(
        adrs=[
            _FakeADR(
                id=uuid4(),
                title="ADR-001: Invoice access via repository",
                status="accepted",
                body="All invoice reads MUST go through InvoiceRepository.",
            ),
        ]
    )
    pr = _minimal_pr(
        changed_files=[
            ChangedFile(path="src/api/customers.py", change_kind="modified"),
        ],
    )
    result = await ConflictDetector().run(_ctx(pr=pr, graph=graph, llm=llm))
    arch = [f for f in result.findings if f.kind == "architectural"]
    assert len(arch) == 1
    assert arch[0].severity == "warning"
    assert "ADR-001" in arch[0].title
    assert arch[0].locations[0].path == "src/api/customers.py"
    assert arch[0].suggested_action and "supersede" in arch[0].suggested_action.lower()


@pytest.mark.asyncio
async def test_architectural_skips_non_accepted_adrs() -> None:
    llm = _FakeLLM()  # no responses — would raise if called
    graph = _FakeGraph(
        adrs=[
            _FakeADR(
                id=uuid4(), title="ADR-XX", status="proposed", body="..."
            ),
            _FakeADR(
                id=uuid4(), title="ADR-YY", status="superseded", body="..."
            ),
        ]
    )
    result = await ConflictDetector().run(_ctx(pr=_minimal_pr(), graph=graph, llm=llm))
    assert [f for f in result.findings if f.kind == "architectural"] == []
    assert result.metadata["architectural"]["adrs_checked"] == 0


@pytest.mark.asyncio
async def test_architectural_skipped_when_embed_prose_missing() -> None:
    """LLM client without ``embed_prose`` → sub-check degrades silently."""

    class _NoEmbedLLM(_FakeLLM):
        embed_prose = None  # type: ignore[assignment]

    llm = _NoEmbedLLM()
    result = await ConflictDetector().run(_ctx(pr=_minimal_pr(), llm=llm))
    assert result.metadata["architectural"]["adrs_checked"] == 0
    assert "unavailable" in (result.metadata["architectural"]["skipped_reason"] or "")


# -- Convention drift ------------------------------------------------------


@pytest.mark.asyncio
async def test_convention_fixture_produces_finding(tmp_path: Path) -> None:
    """Wire the agent at the conflict-repo convention overlay on disk."""

    repo_root = Path(__file__).resolve().parents[3]
    base_dir = repo_root / "fixtures" / "conflict-repo" / "base"
    overlay_dir = repo_root / "fixtures" / "conflict-repo" / "convention"

    # Build a workspace that mimics the convention branch: base files
    # plus the overlay file (refunds.py) on top.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _copy_tree(base_dir, workspace)
    _copy_tree(overlay_dir, workspace)

    pr = _minimal_pr(
        title="Add refund flow",
        body="Adds issue_refund and refund_all_for_customer",
        changed_files=[
            ChangedFile(path="src/billing/refunds.py", change_kind="added"),
        ],
    )

    result = await ConflictDetector().run(
        _ctx(pr=pr, workspace_root=workspace)
    )
    convention = [f for f in result.findings if f.kind == "convention"]
    assert convention  # at least one finding
    assert any(
        f.locations and f.locations[0].path == "src/billing/refunds.py"
        for f in convention
    )
    assert any("BillingError" in (f.suggested_action or "") for f in convention)


def _copy_tree(src: Path, dst: Path) -> None:
    for p in src.rglob("*"):
        if p.is_file():
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())


@pytest.mark.asyncio
async def test_convention_skipped_without_workspace_root() -> None:
    pr = _minimal_pr(
        changed_files=[ChangedFile(path="src/billing/refunds.py", change_kind="added")],
    )
    result = await ConflictDetector().run(_ctx(pr=pr))
    assert [f for f in result.findings if f.kind == "convention"] == []
    assert "workspace_root" in (
        result.metadata["convention"].get("skipped_reason") or ""
    )


# -- Dedup + composition ---------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_semantic_findings_collapsed() -> None:
    """Two callers, same LLM verdict, same location bucket → one entry."""

    sym = ChangedSymbol(
        qualified_name="m.f",
        kind="function",
        change_kind="modified",
        old_signature="def f(x: int) -> int",
        new_signature="def f(x: str) -> int",
        old_source="def f(x: int) -> int:\n    return x\n",
        new_source="def f(x: str) -> int:\n    return int(x)\n",
        file_path="src/m.py",
    )
    graph = _FakeGraph(
        callers={
            "m.f": [
                _FakeSymbolRef(
                    id=uuid4(),
                    qualified_name="m.caller_a",
                    signature="def caller_a() -> int",
                    file_path="src/m.py",
                ),
                _FakeSymbolRef(
                    id=uuid4(),
                    qualified_name="m.caller_b",
                    signature="def caller_b() -> int",
                    file_path="src/m.py",
                ),
            ]
        }
    )
    llm = _FakeLLM()
    # Same verdict twice; dedup key is (kind, title, first-path) so
    # both go into the same bucket and only the first survives.
    for _ in range(2):
        llm.queue(
            SemanticCheckResult,
            SemanticCheckResult(
                compatible=False, reason="int vs str", suggested_fix="cast"
            ),
        )
    result = await ConflictDetector().run(_ctx(pr=_minimal_pr(changed_symbols=[sym]), graph=graph, llm=llm))
    semantic = [f for f in result.findings if f.kind == "semantic"]
    assert len(semantic) == 1  # caller_a survived; caller_b deduped
