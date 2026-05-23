# Mnemos

> AI code review that remembers your codebase.

Mnemos is a GitHub App that watches pull requests and surfaces the conflicts a regular review tool misses: semantic breakage across the call graph, contradictions with past architectural decisions, and drift from your established conventions.

It builds a persistent memory graph of your repository, so every review is informed by every past commit, PR, review comment, and ADR. Self-hosted, open source, Apache 2.0.

## What Mnemos does on v0.1

When you open a pull request, Mnemos posts one comment with three sections.

**Conflict analysis.** Mnemos parses the diff against the graph and flags:
- Symbol signatures that changed in this PR whose callers were not updated
- Renames or deletions that break references elsewhere in the repo
- Changes that contradict an accepted ADR
- New code that departs from a convention used everywhere else in the module

**Context packet.** Before reading the diff, the reviewer sees:
- Up to three past PRs that touched overlapping files or solved similar problems
- The last five commits on each file in the diff
- ADRs that apply to the touched areas
- Issues linked from the PR body, with their current state

**Suggested reviewers.** A ranked shortlist of two to three humans, with a one-line rationale for each. Signals include CODEOWNERS, historical authorship of touched files, review patterns, and current review load. Reviewer load is considered so seniors already drowning in PRs get demoted automatically.

## What Mnemos does not do yet

- Generate merge conflict resolutions
- Write line-level review comments on style or bugs; that is what CodeRabbit, Greptile, and your linter are for
- Reason across multiple repositories
- Support GitLab, Bitbucket, or Gerrit (GitLab is planned for v0.3)

## Quick start

```bash
git clone https://github.com/<you>/mnemos
cd mnemos
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, GITHUB_APP_ID, GITHUB_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET
docker compose up
```

Then create a GitHub App pointing webhooks at `http://<your-host>:3000/api/github/webhooks` and install it on a test repository. The first install triggers an index pass over the repo's recent history; indexing a 100k LOC repo takes around 10 to 20 minutes.

Short install guide: [`INSTALL.md`](INSTALL.md). Full self-hosting walk-through: [`docs/self-hosting.md`](docs/self-hosting.md).

## How it works

Two services, one database.

The TypeScript service is a thin GitHub App layer. It verifies webhook signatures, talks to the GitHub API, posts check runs and comments. It never touches the memory graph directly.

The Python service owns the memory graph and the agents. It exposes a small HTTP API to the TypeScript layer and calls back when a review is ready. Inside the Python service, a coordinator runs three agents concurrently against the PR: the Conflict Detector, the Context Packager, and the Reviewer Router.

The memory graph is Postgres plus pgvector. Nodes for files, symbols, commits, PRs, reviews, persons, ADRs, and issues. Edges for function calls, imports, commit-to-symbol changes, and PR history. Symbols, PRs, and ADRs carry embeddings for similarity search.

Full architecture is in [`docs/architecture.md`](docs/architecture.md).

## Project status

v0.1 is in active development. Phases 1 through 6 are code-complete; Phases 7 (integration polish) and 8 (alpha release) are pending. See [`STATUS.md`](STATUS.md) for the per-area breakdown and [`CHANGELOG.md`](CHANGELOG.md) for shipped work. No version has been tagged yet. Expect breaking schema changes until v0.2.

## Contributing

The project is designed to be extended. Two places contributors add value without touching core code:

- **Writing a new agent.** Subclass `BaseAgent`, implement `run`, register in the agent registry. Guide: [`docs/writing-an-agent.md`](docs/writing-an-agent.md).
- **Adding a language.** Subclass `LanguageParser`, implement symbol, call, and import extraction via tree-sitter. Guide: [`docs/adding-a-language.md`](docs/adding-a-language.md).

Before opening a PR, please read [`CONTRIBUTING.md`](CONTRIBUTING.md) and make sure your change passes the fixture suite in `fixtures/conflict-repo`.

## Benchmarks

Mnemos is benchmarked against a curated fixture suite that covers semantic, architectural, and convention conflicts. Every agent PR must hold or improve the fixture pass rate. See [`fixtures/conflict-repo/README.md`](fixtures/conflict-repo/README.md).

## License

Apache 2.0. See [`LICENSE`](LICENSE).

## Acknowledgements

Mnemos stands on the shoulders of tree-sitter, Probot, SQLAlchemy, pgvector, and the broader ecosystem of AI review tools (CodeRabbit, Greptile, Korbit) that pushed the category forward. Where they stop at line-level review, Mnemos aims to reason about the codebase as a whole.
