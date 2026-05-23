"""Conflict Detector agent and its pure-Python support modules.

Public surface:

- :class:`ConflictDetector` — the agent itself (wired up in a later task)
- :func:`classify_change` — AST-level change classification for a single symbol

Downstream code should prefer the re-exports here over importing from the
internal submodules.
"""

from __future__ import annotations

from codereview.agents.conflict.ast_diff import (
    Classification,
    ClassificationResult,
    classify_change,
)
from codereview.agents.conflict.conventions import (
    ConventionFinding,
    detect_tuple_return_drift,
)
from codereview.agents.conflict.detector import ConflictDetector

__all__ = [
    "Classification",
    "ClassificationResult",
    "ConflictDetector",
    "ConventionFinding",
    "classify_change",
    "detect_tuple_return_drift",
]
