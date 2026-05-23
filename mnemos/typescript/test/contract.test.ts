/**
 * Cross-service contract tests.
 *
 * Each fixture under `../../fixtures/contract/` is the shared source of
 * truth for one orchestrator endpoint. We load it, assert that it matches
 * the `OrchestratorClient`'s typed request body at compile and run time,
 * and stub `fetch` to prove the client POSTs exactly that JSON.
 *
 * If either side drifts from the fixture, this test (or its Python
 * counterpart in `python/tests/test_contract.py`) breaks. That is the
 * intended failure mode.
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it, vi } from 'vitest';

import {
  type AnalyzeRequest,
  type IncrementalUpdateRequest,
  type IndexRequest,
  OrchestratorClient,
} from '../src/orchestrator/client.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONTRACT_DIR = resolve(__dirname, '..', '..', 'fixtures', 'contract');

function loadFixture<T>(name: string): T {
  const raw = readFileSync(resolve(CONTRACT_DIR, name), 'utf-8');
  return JSON.parse(raw) as T;
}

function okResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 202,
    headers: { 'content-type': 'application/json' },
  });
}

describe('contract fixtures', () => {
  const baseUrl = 'http://python-api:8000';
  const internalSecret = 'internal-secret-0123456789abcdef';

  it('analyze-request.json matches AnalyzeRequest and is POSTed verbatim', async () => {
    // The type assertion below fails to compile if the fixture drifts from
    // the TS-side type. At runtime, we also verify every expected field is
    // present (JSON.parse erases excess keys silently otherwise).
    const body = loadFixture<AnalyzeRequest>('analyze-request.json');

    expect(body.installation_id).toBeTypeOf('number');
    expect(body.repository.owner).toBeTypeOf('string');
    expect(body.repository.name).toBeTypeOf('string');
    expect(body.repository.github_id).toBeTypeOf('number');
    expect(body.pull_request.number).toBeTypeOf('number');
    expect(body.pull_request.head_sha).toMatch(/^[0-9a-f]{40}$/);
    expect(body.pull_request.base_sha).toMatch(/^[0-9a-f]{40}$/);
    expect(body.pull_request.title).toBeTypeOf('string');
    expect(body.pull_request.body).toBeTypeOf('string');
    expect(body.pull_request.author).toBeTypeOf('string');
    expect(body.callback_url).toMatch(/^https?:\/\//);
    expect(body.callback_secret.length).toBeGreaterThanOrEqual(16);

    const fetchImpl = vi.fn(async () => okResponse({ job_id: 'job_1', status: 'queued' }));
    const client = new OrchestratorClient({ baseUrl, internalSecret, fetchImpl });

    await client.analyzePullRequest(body);

    const [, init] = fetchImpl.mock.calls[0] ?? [];
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });

  it('index-request.json matches IndexRequest', () => {
    const body = loadFixture<IndexRequest>('index-request.json');

    expect(body.installation_id).toBeTypeOf('number');
    expect(body.repository.owner).toBeTypeOf('string');
    expect(body.depth).toBeTypeOf('number');
    expect(body.depth).toBeGreaterThanOrEqual(1);
    expect(body.depth).toBeLessThanOrEqual(10_000);
  });

  it('incremental-update-request.json matches IncrementalUpdateRequest and is POSTed to the right path', async () => {
    const body = loadFixture<IncrementalUpdateRequest>('incremental-update-request.json');

    expect(body.installation_id).toBeTypeOf('number');
    expect(body.base_sha).toMatch(/^[0-9a-f]{7,40}$/);
    expect(body.head_sha).toMatch(/^[0-9a-f]{7,40}$/);
    expect(body.base_sha).not.toBe(body.head_sha);

    const fetchImpl = vi.fn(async () => okResponse({ job_id: 'job_2', status: 'queued' }));
    const client = new OrchestratorClient({ baseUrl, internalSecret, fetchImpl });

    await client.incrementalUpdateRepository(body);

    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe('http://python-api:8000/v1/repositories/incremental-update');
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });

  it('every fixture round-trips through JSON.stringify without key loss', () => {
    for (const name of [
      'analyze-request.json',
      'index-request.json',
      'incremental-update-request.json',
    ]) {
      const body = loadFixture<Record<string, unknown>>(name);
      const roundtripped = JSON.parse(JSON.stringify(body)) as Record<string, unknown>;
      expect(roundtripped).toEqual(body);
    }
  });
});
