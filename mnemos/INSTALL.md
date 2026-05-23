# Installing Mnemos

A five-minute path for self-hosting on a Linux host. For the full
walk-through with production notes and troubleshooting, see
[`docs/self-hosting.md`](docs/self-hosting.md).

## What you need

- Linux host with Docker 24+ and Docker Compose v2.
- A public-reachable URL for GitHub webhooks (use
  [smee.io](https://smee.io) or ngrok for local testing; any
  HTTPS reverse proxy in production).
- An Anthropic API key.
- Optionally: Voyage (code embeddings), OpenAI (prose embeddings).
  Without these, similarity search falls back to text heuristics.

## 1. Clone and configure

```bash
git clone https://github.com/<you>/mnemos
cd mnemos
cp .env.example .env
```

Open `.env` and fill in, at minimum:

| Variable                 | Purpose                                              |
| ------------------------ | ---------------------------------------------------- |
| `GITHUB_APP_ID`          | The App ID from your GitHub App settings.            |
| `GITHUB_PRIVATE_KEY`     | The private key PEM content (one-line with `\n`).    |
| `GITHUB_WEBHOOK_SECRET`  | The webhook secret you set on the GitHub App.        |
| `ANTHROPIC_API_KEY`      | For the Conflict Detector + Context Packager.        |
| `INTERNAL_SECRET`        | HMAC secret shared between Python and TS services.   |

## 2. Bring up the stack

```bash
docker compose up -d
docker compose logs -f orchestrator
```

Verify:

```bash
curl http://localhost:3000/healthz   # GitHub App (TS)
curl http://localhost:8000/healthz   # Orchestrator (Python)
docker compose exec postgres pg_isready
```

All three should return a 200 / `accepting connections`.

## 3. Create the GitHub App

Visit <https://github.com/settings/apps/new> and use:

- **Webhook URL:** `https://<your-host>/api/github/webhooks`
- **Webhook secret:** the value of `GITHUB_WEBHOOK_SECRET`
- **Permissions:** Contents: read · Pull requests: read+write · Issues: read · Checks: write · Metadata: read
- **Events:** `pull_request`, `push`, `installation`, `installation_repositories`

Copy the App ID and private key back into `.env`, then
`docker compose restart`.

## 4. Install on a test repo

From your App's public page (`https://github.com/apps/<your-app>`),
install it on a single repository. Mnemos kicks off an initial index
pass — about 10-20 minutes for a 100k LOC repo, a few dollars of
embedding spend.

```bash
docker compose logs -f orchestrator | grep index_job
```

## 5. Open a test PR

Once indexing is done, open any PR on the test repo. Within ~45-90
seconds you should see a single Mnemos comment with three sections
(conflicts, context, suggested reviewers) and a green check run.

---

**If something breaks,** the fastest signal is
`docker compose logs -f orchestrator` — every line carries the
`job_id` printed when the PR opened. Full troubleshooting steps and
production deployment notes are in
[`docs/self-hosting.md`](docs/self-hosting.md).
