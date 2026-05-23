/**
 * Smoke tests for the callback server — HMAC verification, body handling,
 * and error surfaces. Matches the intent of `python/tests/test_analyze.py`:
 * wire shape, not business logic.
 */

import { createHmac } from 'node:crypto';
import pino from 'pino';
import { describe, expect, it, vi } from 'vitest';

import { SIGNATURE_HEADER, buildCallbackApp, verifySignature } from '../src/callbacks/review.js';

const silentLogger = pino({ level: 'silent' });

function sign(body: string, secret: string): string {
  return `sha256=${createHmac('sha256', secret).update(body).digest('hex')}`;
}

describe('verifySignature', () => {
  it('accepts a correct signature', () => {
    const secret = 'super-secret-key-16-chars-yes';
    const body = '{"job_id":"abc"}';
    expect(verifySignature(sign(body, secret), body, secret)).toBe(true);
  });

  it('rejects an unknown scheme', () => {
    expect(verifySignature('md5=deadbeef', '{}', 'secret')).toBe(false);
  });

  it('rejects a mismatched signature', () => {
    const body = '{"job_id":"abc"}';
    expect(verifySignature('sha256=00', body, 'secret')).toBe(false);
  });

  it('rejects non-hex payloads without throwing', () => {
    expect(verifySignature('sha256=not-hex', '{}', 'secret')).toBe(false);
  });
});

describe('POST /internal/callback/reviews', () => {
  const secret = 'per-job-secret-0123456789abcdef';

  function buildApp(onReview = vi.fn()) {
    return buildCallbackApp({
      logger: silentLogger,
      lookupSecret: async (jobId) => (jobId === 'job_ok' ? secret : null),
      onReview,
    });
  }

  it('accepts a signed callback and invokes onReview', async () => {
    const onReview = vi.fn();
    const app = buildApp(onReview);

    const body = JSON.stringify({
      job_id: 'job_ok',
      status: 'completed',
      pull_request: { number: 42 },
    });

    const res = await app.request('/internal/callback/reviews', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        [SIGNATURE_HEADER]: sign(body, secret),
      },
      body,
    });

    expect(res.status).toBe(200);
    expect(onReview).toHaveBeenCalledOnce();
    expect(onReview.mock.calls[0]?.[0]?.job_id).toBe('job_ok');
  });

  it('rejects a missing signature', async () => {
    const app = buildApp();
    const res = await app.request('/internal/callback/reviews', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"job_id":"job_ok","status":"completed","pull_request":{"number":1}}',
    });
    expect(res.status).toBe(401);
  });

  it('rejects an unknown job_id', async () => {
    const app = buildApp();
    const body = JSON.stringify({
      job_id: 'job_missing',
      status: 'completed',
      pull_request: { number: 1 },
    });
    const res = await app.request('/internal/callback/reviews', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        [SIGNATURE_HEADER]: sign(body, secret),
      },
      body,
    });
    expect(res.status).toBe(401);
  });

  it('rejects a signature computed with the wrong secret', async () => {
    const app = buildApp();
    const body = JSON.stringify({
      job_id: 'job_ok',
      status: 'completed',
      pull_request: { number: 1 },
    });
    const res = await app.request('/internal/callback/reviews', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        [SIGNATURE_HEADER]: sign(body, 'wrong-secret'),
      },
      body,
    });
    expect(res.status).toBe(401);
  });
});
