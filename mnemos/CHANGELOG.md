# Changelog

All notable changes to Mnemos are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows semantic versioning from `v0.1.0` onward. Schema
breaking changes between `v0.1.x` releases are expected; migrations
will be called out explicitly.

## [Unreleased]

_Nothing yet. File changes targeting `main` will accumulate here._

## [0.1.0-alpha.0] — 2026-04-23

First public alpha. The three v0.1 agents are in place; the full stack
runs under `docker compose up`; the HTTP contract between the Python
orchestrator and the TypeScript GitHub App is contract-tested on both
sides. The laptop acceptance run (install against a real GitHub App,
open a PR, watch the review come back) is still the last mile — feedback
from alpha users drives the `0.1.0-alpha.N` iterations ahead of a tagged
`v0.1.0`.

**Known limitations at alpha.**

- The initial index on a 500k LOC repo can exceed the embedding budget
  and take more than an hour. Mnemos is tuned for 100k LOC and below.
- CODEOWNERS teams surface in the suggested-reviewers section but are
  not expanded to members; we render the team handle verbatim.
- No GitLab / Bitbucket / Gerrit support. GitHub-only for v0.1.
- No merge-conflict resolution. Mnemos flags conflicts; it doesn't fix
  them.
- Only Python language parsing. Other languages need a `LanguageParser`
  contribution — see `docs/adding-a-language.md`.

### Added — Python service

- Memory graph backed by Postgres + pgvector. Models for repositories,
  files, symbols, commits, PRs, reviews, persons, ADRs, and issues;
  edges for function calls, imports, commit-modifies-symbol, and PR
  history. Embeddings for symbols, PRs, and ADRs.
- `index_repo` CLI for seeding the graph from an initial repository
  checkout, with incremental update support.
- Agent framework: `BaseAgent`, `AgentContext`, `AgentResult`,
  `Finding`; coordinator that runs agents concurrently with per-agent
  timeout and error isolation.
- Three v0.1 agents:
  - **Conflict Detector** — AST diff classifier + semantic-conflict
    LLM check, plus ADR-drift and convention heuristics.
  - **Context Packager** — related-PR finder (cosine + Jaccard
    blend), relevant ADR retriever, per-file commit history, linked
    issues, and a risk-notes section, assembled into a context packet
    and summarized in a single LLM call.
  - **Reviewer Router** — CODEOWNERS-aware candidate pool + graph
    signals (authorship share, recent review volume, call-graph
    overlap, acceptance rate, open-PR load) scored by a deterministic
    function. No LLM on the hot path. Load penalty demotes
    overburdened seniors automatically.
- Formatter that shapes agent output into the wire payload the TS
  service expects, including routing of `context_packet` and
  `suggested_reviewers` from agent metadata.
- FastAPI app with `/analyze` entrypoint, HMAC-authenticated
  `/callback` outbound, and an RQ-backed worker for asynchronous
  analysis.
- Prompt registry with versioned `.md` prompts + Pydantic structured
  output schemas.
- 341 unit tests, ruff-clean on the full tree.

### Added — TypeScript service

- Probot-based GitHub App. Handlers for `pull_request`, `push`,
  `installation`, and `installation_repositories`.
- Check-run lifecycle on PR open / synchronize.
- Review-comment formatter that renders the Python callback payload
  into a single PR comment with three sections (conflicts, context,
  suggested reviewers).
- HMAC-verified orchestrator callback receiver.
- 37+ unit tests.

### Added — Infrastructure

- Docker Compose stack (Postgres+pgvector, Redis, Python orchestrator,
  TS GitHub App).
- Python and TypeScript Dockerfiles.
- Alembic initial migration covering every graph table + indexes.
- GitHub Actions CI for both services (ruff + pytest on Python;
  biome + vitest on TypeScript).
- `fixtures/conflict-repo/` — curated synthetic repo with labelled
  conflict branches for the agent fixture suite.
- Architecture, self-hosting, "write an agent", and "add a language"
  documentation.

### Known gaps (work remaining for v0.1)

- Live-app end-to-end acceptance run (Phase 7).
- Seeded-graph live acceptance tests for the three agents (deferred
  from Phases 4/5/6 — needs real Postgres to hit the non-SQLite
  code paths).
- Polish pass on the PR comment formatter + retry/backoff across the
  service boundary (Phase 7).
- Alpha release: versioning, release notes, public App registration
  flow, and onboarding scripts (Phase 8).

See `mnemos-plan/` for per-phase specifications.
