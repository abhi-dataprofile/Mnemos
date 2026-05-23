# Project status

Mnemos is pre-release. `v0.1.0-alpha.0` is ready to tag but hasn't been
pushed to a public remote yet. Expect breaking schema changes until
`v0.2.0`.

## Works today

- **Memory graph.** Indexes a repository into Postgres + pgvector:
  files, symbols, commits, PRs, reviews, persons, ADRs, linked
  issues. Incremental updates on new commits. Tree-sitter-based
  symbol extraction for Python (with a pluggable parser interface
  for future languages).
- **Conflict Detector agent.** Reads the diff, classifies changed
  symbols via AST, cross-checks callers, ADR drift, and convention
  deviations. Flags blocking and warning-level issues.
- **Context Packager agent.** Assembles a "30-second briefing" for
  the reviewer: related PRs (cosine + Jaccard blend), applicable
  ADRs, per-file commit history, linked issues, risk notes. Single
  LLM call for the narrative summary.
- **Reviewer Router agent.** Ranks humans using CODEOWNERS + graph
  signals (authorship, review history, call-graph overlap, acceptance
  rate, load). No LLM. Senior reviewers drowning in PRs get demoted
  automatically.
- **GitHub App layer.** Probot-based. Webhook verification, check
  run lifecycle, review-comment posting with three-section layout.
  PR comment includes severity icons, collapsible long-list blocks,
  a "reviewed by Mnemos" footer, and distinct rendering for CODEOWNERS
  teams.
- **Orchestration.** Coordinator runs agents concurrently with
  per-agent timeout + error isolation. Agent failures degrade the
  review gracefully rather than crashing the job. Indexing-in-progress
  and LLM rate-limit helpers in `codereview.orchestration.degradation`.
- **Observability.** `/metrics` Prometheus scrape with five
  documented metric families; structured JSON logs with a fixed event
  vocabulary (`docs/observability.md`). `/healthz` liveness and
  `/readyz` readiness endpoints.
- **Dockerised stack.** `docker compose up` brings the whole thing up
  with Postgres, Redis, and both services. Every service has a
  healthcheck; inter-service DNS is explicit; a commented resource-
  limits block is ready to uncomment for production.

## Works, but not yet verified against a live install

Unit + integration coverage is dense — 358 Python tests + 56
TypeScript tests, both linters clean — but the end-to-end
acceptance run against a real GitHub App install and a real Postgres
is still pending. That is the last-mile work the alpha window
closes.

## Still to do for v0.1 release

- **Laptop acceptance run.** Fresh clone on a scratch machine,
  install the App on a test repo, open a real PR, watch the full
  three-section review arrive inside 90 seconds. Walks the final
  checklist in `docs/launch.md`.
- **Seeded-graph acceptance tests.** Per-agent tests against a real
  Postgres so the pgvector + union-query paths (which SQLite doesn't
  exercise) are covered too. Tracked under deferred task #46.
- **Alpha onboarding — 5 real repos.** The playbook lives in
  `docs/alpha-onboarding.md`; the outreach, install calls, feedback
  spreadsheet, and two prompt-iteration passes based on real
  feedback are the actual "ship v0.1" work.
- **Release tagging + announcement.** Pre-launch checklist in
  `docs/launch.md`. Push the `v0.1.0-alpha.0` tag, post the three
  channel drafts.

See [`mnemos-plan/`](mnemos-plan/) for the full per-phase spec.
The [`CHANGELOG.md`](CHANGELOG.md) lists shipped work by user-visible
feature.

## Scope boundaries (intentional for v0.1)

- **GitHub only.** No GitLab, Bitbucket, or Gerrit. GitLab is planned
  for v0.3.
- **Python only.** The graph models are language-agnostic; the parser
  is not. Adding Go, TypeScript, etc. happens after v0.1 ships. See
  [`docs/adding-a-language.md`](docs/adding-a-language.md).
- **Three agents only.** New agent types (test-impact, security,
  performance regression) are contributor territory and out of
  v0.1 scope. See [`docs/writing-an-agent.md`](docs/writing-an-agent.md).
- **Self-hosted only.** No hosted Mnemos service is planned for
  v0.1. All state stays on the host you deploy to except the prompt
  payloads your configured LLM sees.
- **No merge-conflict resolution, no line-level bug review.** Mnemos
  is complementary to CodeRabbit / Greptile / linters, not a
  replacement.

## Getting involved

- [Open issues](https://github.com/<you>/mnemos/issues) — bug reports
  and small feature requests welcome.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) for PR workflow and the
  fixture-suite gate.
- [`docs/writing-an-agent.md`](docs/writing-an-agent.md) and
  [`docs/adding-a-language.md`](docs/adding-a-language.md) — the
  two extension points that don't require changing core code.
