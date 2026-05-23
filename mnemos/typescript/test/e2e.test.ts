/**
 * End-to-end integration test for the Phase 3 PR flow.
 *
 * The test stitches the pieces together the way `app.ts` wires them at
 * runtime:
 *
 *   pull_request.opened webhook
 *     → makePullRequestHandler
 *       → createCheckRun (mock Octokit)
 *       → OrchestratorClient.analyzePullRequest (mock fetch)
 *       → jobStore.put
 *   orchestrator fires callback
 *     → startCallbackServer (real Hono server on ephemeral port)
 *       → HMAC verified against the stored per-job secret
 *       → onReview: comment upserted + check run completed
 *
 * The unit tests cover each piece in isolation. This test catches the
 * wiring bugs between them — wrong secret piped through, wrong check_run_id
 * stored, callback path mismatch, signature scheme mismatch, etc.
 */

import { createHmac } from 'node:crypto';
import type { AddressInfo } from 'node:net';

import pino from 'pino';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { type CallbackServer, startCallbackServer } from '../src/callbacks/review.js';
import { formatReviewComment } from '../src/formatters/reviewComment.js';
import { CHECK_RUN_NAME, updateCheckRun } from '../src/github/checks.js';
import { MNEMOS_COMMENT_MARKER, upsertReviewComment } from '../src/github/comments.js';
import { makePullRequestHandler } from '../src/handlers/pullRequest.js';
import { OrchestratorClient } from '../src/orchestrator/client.js';
import { InMemoryJobStore } from '../src/store/jobStore.js';

const silentLogger = pino({ level: 'silent' });

// -- Test doubles -----------------------------------------------------------

/**
 * Minimal Octokit shim covering exactly the calls the Phase 3 code makes:
 *   - checks.create / checks.update
 *   - issues.createComment / issues.updateComment / issues.listComments
 *   - paginate.iterator
 *
 * Every method is a vi.fn so tests can inspect call args; `paginate.iterator`
 * yields an empty list by default (simulating a PR with no prior comment).
 */
function makeFakeOctokit(opts: { existingCommentId?: number } = {}) {
  const checkRunId = 9001;
  const createdCommentId = 55_001;
  const updatedCommentId = 55_002;

  const existingComments = opts.existingCommentId
    ? [{ id: opts.existingCommentId, body: `prior review ${MNEMOS_COMMENT_MARKER}` }]
    : [];

  const checks = {
    create: vi.fn(async () => ({ data: { id: checkRunId } })),
    update: vi.fn(async () => ({ data: { id: checkRunId } })),
  };

  const issues = {
    createComment: vi.fn(async () => ({ data: { id: createdCommentId } })),
    updateComment: vi.fn(async () => ({ data: { id: updatedCommentId } })),
    listComments: vi.fn(),
  };

  // `octokit.paginate.iterator(fn, params)` returns an AsyncIterable of
  // response pages. Our code only reads `page.data`, so a single page is
  // enough for the fake.
  const paginate = {
    iterator: vi.fn(() => ({
      async *[Symbol.asyncIterator]() {
        yield { data: existingComments };
      },
    })),
  };

  return { checks, issues, paginate, _ids: { checkRunId, createdCommentId, updatedCommentId } };
}

type FakeOctokit = ReturnType<typeof makeFakeOctokit>;

/** Probot `Context` shim. Only the fields the handler actually reads. */
function makeFakeContext(
  octokit: FakeOctokit,
  overrides: { action?: 'opened' | 'synchronize' } = {},
) {
  return {
    payload: {
      action: overrides.action ?? 'opened',
      installation: { id: 1234 },
      repository: {
        id: 99_999,
        name: 'monolith',
        full_name: 'acme/monolith',
        default_branch: 'main',
        owner: { login: 'acme' },
      },
      pull_request: {
        number: 317,
        head: { sha: 'f'.repeat(40) },
        base: { sha: '0'.repeat(40) },
        title: 'Split checkout flow',
        body: 'Refactor only. No behaviour change.',
        user: { login: 'abhi' },
      },
    },
    octokit,
    log: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
    // The handler only touches the fields above; a loosely-typed shim is
    // fine here and avoids pulling Probot's deep Webhook type into the test.
  } as unknown as Parameters<ReturnType<typeof makePullRequestHandler>>[0];
}

// -- The test ---------------------------------------------------------------

describe('Phase 3 end-to-end', () => {
  let server: CallbackServer | null = null;

  afterEach(async () => {
    if (server) {
      await server.close();
      server = null;
    }
  });

  /**
   * Walks one PR through the full flow. Shared between the two scenarios
   * (new comment vs. update existing) because the assertions are identical
   * except for which issues.* method was hit.
   */
  async function runFlow(opts: { existingCommentId?: number; blocking: boolean }): Promise<{
    octokit: FakeOctokit;
    capturedOrchestratorBody: unknown;
    callbackStatus: number;
  }> {
    // 1. Real orchestrator client, fake fetch. Capture the analyze body
    //    so we can sign the callback with the same secret later.
    let capturedBody: Record<string, unknown> | null = null;
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      capturedBody = JSON.parse(init?.body as string) as Record<string, unknown>;
      return new Response(JSON.stringify({ job_id: 'job_abc123', status: 'queued' }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      });
    });

    const orchestrator = new OrchestratorClient({
      baseUrl: 'http://python-api:8000',
      internalSecret: 'internal-secret-0123456789abcdef',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    const jobStore = new InMemoryJobStore();

    // 2. Fake Octokit + handler.
    const octokit = makeFakeOctokit({ existingCommentId: opts.existingCommentId });
    const handler = makePullRequestHandler({
      orchestrator,
      jobStore,
      config: { baseUrl: 'http://mnemos-app:3001' },
    });

    await handler(makeFakeContext(octokit));

    // 3. Handler assertions — check run created, orchestrator called,
    //    job record stored with the same secret that went to the orchestrator.
    expect(octokit.checks.create).toHaveBeenCalledOnce();
    const createArgs = octokit.checks.create.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(createArgs).toMatchObject({
      owner: 'acme',
      repo: 'monolith',
      name: CHECK_RUN_NAME,
      head_sha: 'f'.repeat(40),
      status: 'in_progress',
    });

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(capturedBody).not.toBeNull();
    const analyzeBody = capturedBody as {
      installation_id: number;
      callback_url: string;
      callback_secret: string;
    };
    expect(analyzeBody.installation_id).toBe(1234);
    expect(analyzeBody.callback_url).toBe('http://mnemos-app:3001/internal/callback/reviews');

    const secret = analyzeBody.callback_secret;
    expect(typeof secret).toBe('string');
    expect(secret.length).toBeGreaterThanOrEqual(32);

    const record = await jobStore.get('job_abc123');
    expect(record).not.toBeNull();
    expect(record?.callback_secret).toBe(secret);
    expect(record?.check_run_id).toBe(octokit._ids.checkRunId);
    expect(record?.pr_number).toBe(317);

    // 4. Stand up the real callback server on an ephemeral port so the HMAC
    //    path runs end-to-end, not just through in-process `app.request`.
    server = startCallbackServer({
      port: 0,
      lookupSecret: jobStore.lookupSecret,
      logger: silentLogger,
      // Reproduce the onReview wiring from app.ts — this is exactly the
      // glue that the unit tests do not cover.
      onReview: async (body) => {
        const rec = await jobStore.get(body.job_id);
        if (rec === null) return;
        try {
          // In production this is `probot.auth(installation_id)`. The
          // integration point we want to cover is the octokit call shape,
          // not Probot's auth — so we stub by returning the fake directly.
          const installationOctokit = octokit as unknown as Parameters<
            typeof upsertReviewComment
          >[0];
          const review = (body.review ?? {}) as Parameters<typeof formatReviewComment>[0];
          const markdown = formatReviewComment(review);
          await upsertReviewComment(installationOctokit, {
            owner: rec.owner,
            repo: rec.repo,
            issue_number: rec.pr_number,
            body: markdown,
          });
          const conclusion = (review.conflicts ?? []).some((c) => c.severity === 'blocking')
            ? ('action_required' as const)
            : ('neutral' as const);
          await updateCheckRun(installationOctokit, {
            owner: rec.owner,
            repo: rec.repo,
            check_run_id: rec.check_run_id,
            status: 'completed',
            conclusion,
            output: {
              title: 'Mnemos review complete',
              summary: review.summary ?? 'See the PR comment for details.',
            },
          });
        } finally {
          await jobStore.clear(body.job_id);
        }
      },
    });

    const addr = server.server.address() as AddressInfo;
    const callbackUrl = `http://127.0.0.1:${addr.port}/internal/callback/reviews`;

    // 5. Build the callback body the orchestrator would send and sign it
    //    with the same secret it received.
    const callbackBody = JSON.stringify({
      job_id: 'job_abc123',
      status: 'completed',
      pull_request: { number: 317 },
      review: {
        summary: 'Looks good; two minor notes.',
        conflicts: opts.blocking
          ? [
              {
                severity: 'blocking',
                title: 'Auth middleware removed',
                description: 'This contradicts ADR-0004.',
              },
            ]
          : [],
        context: [],
        suggested_reviewers: [],
      },
    });
    const signature = `sha256=${createHmac('sha256', secret).update(callbackBody).digest('hex')}`;

    const callbackRes = await fetch(callbackUrl, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-mnemos-signature': signature,
      },
      body: callbackBody,
    });

    return {
      octokit,
      capturedOrchestratorBody: analyzeBody,
      callbackStatus: callbackRes.status,
    };
  }

  it('posts a new comment and marks the check neutral when no conflicts block', async () => {
    const result = await runFlow({ blocking: false });

    expect(result.callbackStatus).toBe(200);

    // New PR → createComment hit, not updateComment.
    expect(result.octokit.issues.createComment).toHaveBeenCalledOnce();
    expect(result.octokit.issues.updateComment).not.toHaveBeenCalled();

    const commentArgs = result.octokit.issues.createComment.mock.calls[0]?.[0] as {
      body: string;
      issue_number: number;
    };
    expect(commentArgs.issue_number).toBe(317);
    expect(commentArgs.body).toContain(MNEMOS_COMMENT_MARKER);
    expect(commentArgs.body).toContain('Looks good; two minor notes.');

    // Check run flipped to completed + neutral.
    expect(result.octokit.checks.update).toHaveBeenCalledOnce();
    const updateArgs = result.octokit.checks.update.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(updateArgs).toMatchObject({
      owner: 'acme',
      repo: 'monolith',
      check_run_id: 9001,
      status: 'completed',
      conclusion: 'neutral',
    });
  });

  it('updates the existing comment and marks the check action_required on a blocking conflict', async () => {
    const result = await runFlow({ existingCommentId: 77_777, blocking: true });

    expect(result.callbackStatus).toBe(200);

    // PR with prior comment → updateComment hit, not createComment.
    expect(result.octokit.issues.updateComment).toHaveBeenCalledOnce();
    expect(result.octokit.issues.createComment).not.toHaveBeenCalled();

    const updateCommentArgs = result.octokit.issues.updateComment.mock.calls[0]?.[0] as {
      comment_id: number;
      body: string;
    };
    expect(updateCommentArgs.comment_id).toBe(77_777);
    expect(updateCommentArgs.body).toContain(MNEMOS_COMMENT_MARKER);

    const updateArgs = result.octokit.checks.update.mock.calls[0]?.[0] as { conclusion: string };
    expect(updateArgs.conclusion).toBe('action_required');
  });

  it('rejects a callback signed with the wrong secret and leaves the PR untouched', async () => {
    // Spin up the flow up to the callback step, then send a bad signature.
    let capturedBody: Record<string, unknown> | null = null;
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      capturedBody = JSON.parse(init?.body as string) as Record<string, unknown>;
      return new Response(JSON.stringify({ job_id: 'job_xyz', status: 'queued' }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      });
    });
    const orchestrator = new OrchestratorClient({
      baseUrl: 'http://python-api:8000',
      internalSecret: 'internal-secret-0123456789abcdef',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    const jobStore = new InMemoryJobStore();
    const octokit = makeFakeOctokit();
    const handler = makePullRequestHandler({
      orchestrator,
      jobStore,
      config: { baseUrl: 'http://mnemos-app:3001' },
    });
    await handler(makeFakeContext(octokit));
    expect(capturedBody).not.toBeNull();

    server = startCallbackServer({
      port: 0,
      lookupSecret: jobStore.lookupSecret,
      logger: silentLogger,
      onReview: vi.fn(),
    });
    const addr = server.server.address() as AddressInfo;
    const callbackBody = JSON.stringify({
      job_id: 'job_xyz',
      status: 'completed',
      pull_request: { number: 317 },
    });
    const badSig = `sha256=${createHmac('sha256', 'not-the-real-secret').update(callbackBody).digest('hex')}`;

    const res = await fetch(`http://127.0.0.1:${addr.port}/internal/callback/reviews`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-mnemos-signature': badSig,
      },
      body: callbackBody,
    });

    expect(res.status).toBe(401);
    // Check run was created at handler time; it must NOT have been updated
    // by the rejected callback.
    expect(octokit.checks.create).toHaveBeenCalledOnce();
    expect(octokit.checks.update).not.toHaveBeenCalled();
    expect(octokit.issues.createComment).not.toHaveBeenCalled();
  });
});
