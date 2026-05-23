# @mnemos/github-app

The thin GitHub-facing layer of Mnemos. Receives webhooks via Probot, forwards
pull-request events to the Python orchestrator, and posts the resulting review
back to GitHub as a check run and/or comment.

## Layout

```
src/
├── app.ts                 Probot entry, wires handlers onto the app
├── config.ts              env loading with a zod schema
├── handlers/              webhook handlers (pull_request, push, installation)
├── github/                Octokit helpers (PR data, check runs, comments)
├── orchestrator/          signed client that POSTs to the Python API
└── callbacks/             HMAC-verified callback server (hono)
```

`src/app.ts` boots Probot on one port; `src/callbacks` runs a small hono
server on a second port inside the same container. Both are supervised by
`node dist/app.js` in production.

## Scripts

- `npm run dev` — run with `tsx watch`
- `npm run build` — emit `dist/`
- `npm run typecheck` — `tsc --noEmit`
- `npm run lint` / `npm run format` — Biome
- `npm test` — vitest

## Phase 1 status

Package config, tsconfig, and layout land in this phase. Handler bodies and
the orchestrator client arrive in Phase 1's follow-on task (see
`mnemos-plan/02-phase-1-scaffolding.md`).
