/**
 * Verify that the orchestrator client POSTs to the right path, sends the
 * bearer header, and surfaces non-2xx responses as `OrchestratorError`.
 */

import { describe, expect, it, vi } from 'vitest';

import {
  OrchestratorClient,
  OrchestratorError,
  generateCallbackSecret,
} from '../src/orchestrator/client.js';

function okResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 202,
    headers: { 'content-type': 'application/json' },
  });
}

describe('OrchestratorClient', () => {
  const baseUrl = 'http://python-api:8000/';
  const internalSecret = 'internal-secret-0123456789abcdef';

  const body = {
    installation_id: 1,
    repository: { owner: 'acme', name: 'monolith', github_id: 42 },
    pull_request: {
      number: 1,
      head_sha: 'a'.repeat(40),
      base_sha: 'b'.repeat(40),
      title: 't',
      body: '',
      author: 'abhi',
    },
    callback_url: 'http://ts-app:3001/internal/callback/reviews',
    callback_secret: 'c'.repeat(32),
  } as const;

  it('POSTs the analyze endpoint with bearer auth and trims the base URL', async () => {
    const fetchImpl = vi.fn(async () => okResponse({ job_id: 'job_xyz', status: 'queued' }));
    const client = new OrchestratorClient({ baseUrl, internalSecret, fetchImpl });

    const res = await client.analyzePullRequest(body);

    expect(res).toEqual({ job_id: 'job_xyz', status: 'queued' });
    expect(fetchImpl).toHaveBeenCalledOnce();
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe('http://python-api:8000/v1/pull-requests/analyze');
    expect(init?.method).toBe('POST');
    const headers = init?.headers as Record<string, string>;
    expect(headers.authorization).toBe(`Bearer ${internalSecret}`);
    expect(headers['content-type']).toBe('application/json');
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });

  it('throws OrchestratorError on non-2xx', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response('boom', {
          status: 500,
          headers: { 'content-type': 'text/plain' },
        }),
    );
    const client = new OrchestratorClient({ baseUrl, internalSecret, fetchImpl });

    await expect(client.analyzePullRequest(body)).rejects.toBeInstanceOf(OrchestratorError);
  });
});

describe('generateCallbackSecret', () => {
  it('produces 64 hex chars (32 bytes), well above the 16-char floor', () => {
    const secret = generateCallbackSecret();
    expect(secret).toMatch(/^[0-9a-f]{64}$/);
  });

  it('is unique across calls', () => {
    expect(generateCallbackSecret()).not.toBe(generateCallbackSecret());
  });
});
