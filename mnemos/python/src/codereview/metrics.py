"""Prometheus metric instruments for the orchestrator.

All metric names follow the ``mnemos_`` prefix documented in
``docs/observability.md``. Label cardinality is kept deliberately low:
agent names come from a finite registry, model names from a finite
provider list, so they're safe as labels. Repository IDs stay out of
labels — they'd blow up the cardinality on any multi-tenant install.

Instruments are defined at module scope so every call site imports the
same singletons. The default CollectorRegistry is used so
``prometheus_client.generate_latest()`` picks them up without extra
wiring.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "AGENT_FAILURES",
    "GRAPH_QUERY_DURATION",
    "INDEX_PROGRESS",
    "LLM_TOKENS",
    "REVIEW_DURATION",
]


# Latency of a whole review from analyze-request to callback. Buckets
# chosen for a 90-second SLO — below 5s is "fast", 30-90s is the
# expected range for medium PRs, over 120s is a tail incident.
REVIEW_DURATION: Histogram = Histogram(
    "mnemos_review_duration_seconds",
    "End-to-end PR review duration (analyze -> callback).",
    labelnames=("status",),
    buckets=(1.0, 5.0, 15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 300.0),
)

# Count of agent failures — timeouts, exceptions, budget overruns. A
# rate of failures-per-review above a few percent is an incident
# signal.
AGENT_FAILURES: Counter = Counter(
    "mnemos_agent_failures_total",
    "Agent failures grouped by reason.",
    labelnames=("agent", "reason"),
)

# LLM token consumption. Split by model and input/output so cost
# dashboards can slice either way.
LLM_TOKENS: Counter = Counter(
    "mnemos_llm_tokens_total",
    "LLM tokens used, by model and direction.",
    labelnames=("model", "type"),
)

# Duration of graph queries. Keep the method label tight — only the
# named public queries on GraphClient, not every internal helper.
GRAPH_QUERY_DURATION: Histogram = Histogram(
    "mnemos_graph_query_duration_seconds",
    "Graph query latency.",
    labelnames=("method",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Ratio 0.0..1.0 of repo indexing progress. The indexer updates this
# gauge as it walks commits; a dashboard plots it per repo.
INDEX_PROGRESS: Gauge = Gauge(
    "mnemos_index_progress_ratio",
    "Fraction (0.0-1.0) of the initial index completed.",
    labelnames=("repo",),
)
