/**
 * Tests for the push handler.
 *
 * The handler turns a `push` webhook on the default branch into a
 * `POST /v1/repositories/incremental-update` against the orchestrator.
 * Topic-branch pushes, deletes, and zero-SHA force-pushes are skipped.
 */

import { describe, expect, it, vi } from 'vitest';

import { makePushHandler } from '../src/handlers/push.js';
import { OrchestratorError } from '../src/orchestrator/client.js';

interface FakeContext {
  payload: unknown;
  log: {
    info: ReturnType<typeof vi.fn>;
    warn: ReturnType<typeof vi.fn>;
    debug: ReturnType<typeof vi.fn>;
    error: ReturnType<typeof vi.fn>;
  };
}

function makeContext(overrides: Partial<unknown> = {}): FakeContext {
  return {
    payload: {
      ref: 'refs/heads/main',
      before: 'a'.repeat(40),
      after: 'b'.repeat(40),
      deleted: false,
      installation: { id: 12345 },
      repository: {
        id: 7,
        name: 'monolith',
        full_name: 'acme/monolith',
        owner: { login: 'acme' },
        default_branch: 'main',
      },
      commits: [{ id: 'c1' }, { id: 'c2' }],
      ...overrides,
    },
    log: {
      info: vi.fn(),
      warn: vi.fn(),
      debug: vi.fn(),
      error: vi.fn(),
    },
  };
}

function makeOrchestrator(impl?: (...args: unknown[]) => unknown) {
  const fn = vi.fn(impl ?? (async () => ({ job_id: 'job_xyz', status: 'queued' })));
  return { orchestrator: { incrementalUpdateRepository: fn } as unknown, fn };
}

describe('push handler', () => {
  it('forwards default-branch pushes to /incremental-update', async () => {
    const { orchestrator, fn } = makeOrchestrator();
    const handler = makePushHandler({ orchestrator });
    const ctx = makeContext();

    await handler(ctx as unknown);

    expect(fn).toHaveBeenCalledOnce();
    const body = fn.mock.calls[0]?.[0];
    expect(body).toMatchObject({
      installation_id: 12345,
      repository: { owner: 'acme', name: 'monolith', github_id: 7 },
      base_sha: 'a'.repeat(40),
      head_sha: 'b'.repeat(40),
    });
  });

  it('ignores pushes to non-default branches', async () => {
    const { orchestrator, fn } = makeOrchestrator();
    const handler = makePushHandler({ orchestrator });
    const ctx = makeContext({ ref: 'refs/heads/feature-x' });

    await handler(ctx as unknown);

    expect(fn).not.toHaveBeenCalled();
  });

  it('ignores deleted refs', async () => {
    const { orchestrator, fn } = makeOrchestrator();
    const handler = makePushHandler({ orchestrator });
    const ctx = makeContext({ deleted: true });

    await handler(ctx as unknown);

    expect(fn).not.toHaveBeenCalled();
  });

  it('skips zero-SHA "before" (force push / new branch)', async () => {
    const { orchestrator, fn } = makeOrchestrator();
    const handler = makePushHandler({ orchestrator });
    const ctx = makeContext({ before: '0'.repeat(40) });

    await handler(ctx as unknown);

    expect(fn).not.toHaveBeenCalled();
  });

  it('warns and bails on missing installation id', async () => {
    const { orchestrator, fn } = makeOrchestrator();
    const handler = makePushHandler({ orchestrator });
    const ctx = makeContext({ installation: undefined });

    await handler(ctx as unknown);

    expect(fn).not.toHaveBeenCalled();
    expect(ctx.log.warn).toHaveBeenCalled();
  });

  it('logs but does not throw on OrchestratorError', async () => {
    const { orchestrator } = makeOrchestrator(async () => {
      throw new OrchestratorError(503, 'overloaded');
    });
    const handler = makePushHandler({ orchestrator });
    const ctx = makeContext();

    await expect(handler(ctx as unknown)).resolves.toBeUndefined();
    expect(ctx.log.error).toHaveBeenCalled();
  });
});
