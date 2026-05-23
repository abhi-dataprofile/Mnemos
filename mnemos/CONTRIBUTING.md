# Contributing to Mnemos

Thanks for considering a contribution. Mnemos is deliberately designed to be extended in two places without touching core code: new agents and new language parsers. Most of this guide exists so those contributions are easy.

Everything below assumes you have read the root [`README.md`](README.md) and, for anything non-trivial, the [`docs/architecture.md`](docs/architecture.md) reference.

## Running the fixture suite locally

The fixture suite is the quality gate for every agent PR. If it does not pass or improve against your branch, the PR is not ready.

First, build the fixture git repo:

```bash
cd fixtures/conflict-repo
./setup.sh /tmp/conflict-repo
```

This produces `/tmp/conflict-repo` with one `main` branch and three conflict branches (`conflict/semantic`, `conflict/architectural`, `conflict/convention`). Each conflict branch contains one commit that models one of the three conflict types Mnemos detects.

Then run the agent harness:

```bash
cd python
pytest tests/agents/ --fixture-repo=/tmp/conflict-repo
```

The harness indexes the repo, runs the three agents against each branch as if it were an open PR, and checks the findings against `fixtures/conflict-repo/expected/*.json`. Extra findings are allowed; missing findings are failures.

Every PR into `main` must keep the fixture pass rate at or above the pre-PR baseline. CI enforces this.

## Proposing a new agent

An agent is a Python class that subclasses `BaseAgent`, implements `run(ctx) -> AgentResult`, and is listed in the agent registry. The full narrative walkthrough is in [`docs/writing-an-agent.md`](docs/writing-an-agent.md).

Before you start:

1. Open an issue describing the signal your agent detects, the shape of its findings, and an example PR where it would fire. Agents with overlapping responsibilities dilute each other; we will push back if the idea duplicates an existing agent.
2. If the agent needs a new graph question, plan for a `GraphClient` method too. Raw SQL inside an agent is rejected on review.

## Proposing a new language

Mnemos v0.1 is Python-only. TypeScript and JavaScript are planned for v0.2; other languages land as contributors add them. The narrative walkthrough is in [`docs/adding-a-language.md`](docs/adding-a-language.md).

Before you start:

1. Open an issue confirming which tree-sitter grammar you plan to use.
2. Plan to extend the fixture suite: at minimum a `base/` snippet and a `semantic/` branch that breaks a caller. Language support without a fixture is not mergeable.

## Code style

**Python (orchestrator service).**

- Format and lint with [Ruff](https://docs.astral.sh/ruff/). `ruff check .` and `ruff format .` must be clean before push.
- Type everything. `mypy --strict` in CI.
- Async first. Anything that touches Postgres, Redis, the GitHub API, or the LLM client is `async def`.
- Pydantic for all data schemas that cross a boundary (LLM output, HTTP, graph row to model).

**TypeScript (GitHub App service).**

- Format with [Biome](https://biomejs.dev/) (preferred) or Prettier. Lint with Biome or ESLint.
- Strict TypeScript (`"strict": true`). No `any` in app code.
- Probot handlers stay thin; anything >100 lines moves to the orchestrator.

## Commit convention

We follow [Conventional Commits](https://www.conventionalcommits.org):

```
<type>(<optional scope>): <short summary>

<optional body>

<optional footer>
```

Types we use:

| Type | Use |
|---|---|
| `feat` | New feature or capability visible to the user or contributor. |
| `fix` | Bug fix. |
| `refactor` | Rewrite without behavior change. |
| `docs` | Docs-only change. |
| `test` | Test-only change. |
| `chore` | Tooling, dependencies, CI, non-code glue. |
| `perf` | Performance improvement without behavior change. |

Scope is optional but encouraged. Examples: `feat(conflict-detector): detect return-type narrowing`, `docs(architecture): clarify per-PR snapshot`, `fix(parser-python): handle decorated methods`.

## PR checklist

Every PR must, before request for review:

- [ ] Fixture suite passes. Paste the relevant output into the PR description if the change affects agents.
- [ ] New code has tests. Unit tests live next to the module; agent tests plug into the fixture harness.
- [ ] `docs/architecture.md` is updated if the schema changed, a new graph edge was added, or a new HTTP endpoint was introduced.
- [ ] `.env.example` is updated if a new environment variable was added. `git grep` must not find an env var missing from the template.
- [ ] If the change touches `mnemos-plan/`, the corresponding phase doc's "notes" section was updated.
- [ ] Commit messages follow the convention above.

## Code of conduct

We follow the [Contributor Covenant, v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Harassment, discriminatory behavior, or personal attacks are grounds for removal from the project. Unacceptable behavior can be reported to the maintainer email listed in `README.md`.

## Licensing

All contributions are licensed under Apache 2.0 (see [`LICENSE`](LICENSE)). By opening a PR you agree that your contribution is provided under that license, with the patent grant it includes. We do not require a separate CLA.

## Getting help

Open a discussion on GitHub if you are unsure where to start, or file an issue tagged `question`. We prefer public threads over DMs so answers help future contributors.
