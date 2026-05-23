/**
 * Config parsing — surface missing/invalid env before anything else boots.
 */

import { describe, expect, it } from 'vitest';

import { loadConfig } from '../src/config.js';

const validEnv = {
  NODE_ENV: 'test',
  PORT: '3000',
  LOG_LEVEL: 'info',
  CALLBACK_PORT: '3001',
  GITHUB_APP_ID: '12345',
  GITHUB_PRIVATE_KEY: '-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----',
  GITHUB_WEBHOOK_SECRET: 'webhook-secret',
  ORCHESTRATOR_URL: 'http://python-api:8000',
  INTERNAL_SECRET: 'internal-secret-0123456789abcdef',
  BASE_URL: 'http://ts-app:3001',
} satisfies NodeJS.ProcessEnv;

describe('loadConfig', () => {
  it('parses a valid environment', () => {
    const cfg = loadConfig(validEnv);
    expect(cfg.port).toBe(3000);
    expect(cfg.callbackPort).toBe(3001);
    expect(cfg.orchestratorBaseUrl).toBe('http://python-api:8000');
  });

  it('throws when INTERNAL_SECRET is too short', () => {
    expect(() => loadConfig({ ...validEnv, INTERNAL_SECRET: 'short' })).toThrow(/INTERNAL_SECRET/);
  });

  it('throws when a required variable is missing', () => {
    const { GITHUB_APP_ID: _discarded, ...rest } = validEnv;
    expect(() => loadConfig(rest)).toThrow(/Invalid configuration/);
  });

  it('defaults PORT and CALLBACK_PORT when not set', () => {
    const { PORT: _p, CALLBACK_PORT: _c, ...rest } = validEnv;
    const cfg = loadConfig(rest);
    expect(cfg.port).toBe(3000);
    expect(cfg.callbackPort).toBe(3001);
  });
});
