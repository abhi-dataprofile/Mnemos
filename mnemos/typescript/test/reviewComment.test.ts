/**
 * Unit tests for the review comment formatter.
 *
 * Covers the contract from `docs/architecture.md` — three sections in a
 * fixed order, severity-sorted conflicts, collapsible context, ranked
 * reviewers — plus the graceful-degradation paths for malformed input.
 */

import { describe, expect, it } from 'vitest';

import {
  type Conflict,
  type Review,
  type SuggestedReviewer,
  formatReviewComment,
  renderConflicts,
  renderContext,
  renderSuggestedReviewers,
} from '../src/formatters/reviewComment.js';

describe('formatReviewComment', () => {
  it('renders all three sections in order', () => {
    const review: Review = {
      summary: 'Three findings.',
      conflicts: [
        { severity: 'warning', title: 'Naming drift', detail: 'snake vs camel' },
        { severity: 'blocking', title: 'Missing caller update' },
      ],
      context: {
        narrative: 'Touches billing.',
        related_prs: [{ number: 38, title: 'Split invoices', url: 'https://x/38' }],
        related_adrs: [{ title: 'ADR-001 Repository pattern' }],
      },
      suggested_reviewers: [
        { login: 'pranav', score: 0.82, rationale: 'owns this file' },
        { login: 'chad', score: 0.61 },
      ],
    };

    const out = formatReviewComment(review);

    // Section order: Conflicts → Context → Suggested reviewers
    const conflictIdx = out.indexOf('### Conflicts');
    const contextIdx = out.indexOf('### Context');
    const reviewerIdx = out.indexOf('### Suggested reviewers');
    expect(conflictIdx).toBeGreaterThanOrEqual(0);
    expect(contextIdx).toBeGreaterThan(conflictIdx);
    expect(reviewerIdx).toBeGreaterThan(contextIdx);

    // Summary appears near the top, before the first section.
    expect(out.indexOf('Three findings.')).toBeGreaterThanOrEqual(0);
    expect(out.indexOf('Three findings.')).toBeLessThan(conflictIdx);
  });

  it('handles a completely empty review without throwing', () => {
    const out = formatReviewComment({});
    expect(out).toContain('## Mnemos review');
    expect(out).toContain('_No conflicts detected._');
    // Empty context / reviewers sections are omitted rather than left blank.
    expect(out).not.toContain('### Context');
    expect(out).not.toContain('### Suggested reviewers');
  });

  it('renders a failed-agent footer when agents crashed', () => {
    const out = formatReviewComment({ failed_agents: ['context_packager'] });
    expect(out).toContain('`context_packager` failed');
  });

  it('pluralises the failed-agent footer correctly', () => {
    const out = formatReviewComment({ failed_agents: ['a', 'b'] });
    expect(out).toContain('Agents `a`, `b` failed');
  });
});

describe('renderConflicts', () => {
  it('sorts blocking before warning before info', () => {
    const conflicts: Conflict[] = [
      { severity: 'info', title: 'I' },
      { severity: 'blocking', title: 'B' },
      { severity: 'warning', title: 'W' },
    ];
    const out = renderConflicts(conflicts);
    const bIdx = out.indexOf('[Blocking]');
    const wIdx = out.indexOf('[Warning]');
    const iIdx = out.indexOf('[Info]');
    expect(bIdx).toBeGreaterThanOrEqual(0);
    expect(wIdx).toBeGreaterThan(bIdx);
    expect(iIdx).toBeGreaterThan(wIdx);
  });

  it('prefixes known severities with a GitHub-rendered icon', () => {
    const conflicts: Conflict[] = [
      { severity: 'blocking', title: 'B' },
      { severity: 'warning', title: 'W' },
      { severity: 'info', title: 'I' },
    ];
    const out = renderConflicts(conflicts);
    expect(out).toContain(':no_entry: [Blocking]');
    expect(out).toContain(':warning: [Warning]');
    expect(out).toContain(':information_source: [Info]');
  });

  it('omits the icon for unknown severities', () => {
    const out = renderConflicts([{ severity: 'mystery', title: 'X' } as Conflict]);
    // Title still renders — just without a prefix icon.
    expect(out).toContain('[Mystery]');
    expect(out).not.toContain(':no_entry: [Mystery]');
  });

  it('places unknown severities after known ones', () => {
    const conflicts: Conflict[] = [
      { severity: 'mystery', title: 'X' } as Conflict,
      { severity: 'blocking', title: 'B' },
    ];
    const out = renderConflicts(conflicts);
    expect(out.indexOf('[Blocking]')).toBeLessThan(out.indexOf('[Mystery]'));
  });

  it('renders locations, related symbols, and suggested action when present', () => {
    const conflicts: Conflict[] = [
      {
        severity: 'blocking',
        title: 'generate_pdf signature changed',
        detail: 'Caller not updated',
        locations: [{ path: 'src/api/invoices.py', line: 154 }, { path: 'src/legacy/old.py' }],
        related_symbols: ['billing.invoice.generate_pdf'],
        suggested_action: 'Update the call site to pass the Invoice object',
      },
    ];
    const out = renderConflicts(conflicts);
    expect(out).toContain('`src/api/invoices.py:154`');
    expect(out).toContain('`src/legacy/old.py`');
    expect(out).toContain('`billing.invoice.generate_pdf`');
    expect(out).toContain('Update the call site');
    expect(out).toContain('Caller not updated');
  });

  it('escapes markdown characters in conflict titles', () => {
    const out = renderConflicts([{ severity: 'info', title: 'a_b_c*d' }]);
    // Underscores and asterisks should be escaped so the bold wrapper
    // doesn't break or emit spurious emphasis.
    expect(out).toContain('a\\_b\\_c\\*d');
  });

  it('handles the empty case with an explicit "no conflicts" note', () => {
    expect(renderConflicts([])).toBe('### Conflicts\n\n_No conflicts detected._');
  });

  it('collapses location lists longer than 3 items into a details block', () => {
    const conflicts: Conflict[] = [
      {
        severity: 'blocking',
        title: 'broad impact',
        locations: [
          { path: 'a.py', line: 1 },
          { path: 'b.py', line: 2 },
          { path: 'c.py', line: 3 },
          { path: 'd.py', line: 4 },
          { path: 'e.py', line: 5 },
        ],
      },
    ];
    const out = renderConflicts(conflicts);
    expect(out).toContain('<details>');
    expect(out).toContain('<summary>Locations (5)</summary>');
    expect(out).toContain('`a.py:1`');
    expect(out).toContain('`e.py:5`');
  });

  it('keeps short location lists inline (no details wrapper)', () => {
    const conflicts: Conflict[] = [
      {
        severity: 'blocking',
        title: 'narrow',
        locations: [{ path: 'a.py', line: 1 }, { path: 'b.py' }],
      },
    ];
    const out = renderConflicts(conflicts);
    expect(out).not.toContain('<details>');
    expect(out).toContain('Locations:');
  });

  it('renders multi-line suggested_action as a fenced code block', () => {
    const conflicts: Conflict[] = [
      {
        severity: 'warning',
        title: 't',
        suggested_action: 'Run the following:\n  cd refunds\n  rg BillingError',
      },
    ];
    const out = renderConflicts(conflicts);
    expect(out).toContain('**Suggested action:**\n\n```');
    expect(out).toContain('cd refunds');
  });

  it('collapses related_symbols lists longer than 3 items into a details block', () => {
    const conflicts: Conflict[] = [
      {
        severity: 'warning',
        title: 't',
        related_symbols: ['a.x', 'b.y', 'c.z', 'd.w', 'e.v'],
      },
    ];
    const out = renderConflicts(conflicts);
    expect(out).toContain('<summary>Related symbols (5)</summary>');
    expect(out).toContain('`a.x`');
  });
});

describe('renderContext', () => {
  it('returns an empty string when context is missing or empty', () => {
    expect(renderContext(undefined)).toBe('');
    expect(renderContext({})).toBe('');
    expect(renderContext({ related_prs: [], related_adrs: [], recent_commits: [] })).toBe('');
  });

  it('wraps non-empty context in a collapsible <details> block', () => {
    const out = renderContext({
      narrative: 'Small blurb.',
      related_prs: [{ number: 1, title: 't' }],
    });
    expect(out).toContain('<details>');
    expect(out).toContain('</details>');
    expect(out).toContain('<summary>Background for this change</summary>');
    expect(out).toContain('Small blurb.');
    expect(out).toContain('#1');
  });

  it('shortens commit SHAs to 7 chars', () => {
    const out = renderContext({
      recent_commits: [{ sha: 'abcdef1234567890', title: 'fix thing' }],
    });
    expect(out).toContain('`abcdef1`');
    expect(out).not.toContain('abcdef1234567890');
  });
});

describe('renderSuggestedReviewers', () => {
  it('sorts by descending score and formats @-mentions with scores', () => {
    const reviewers: SuggestedReviewer[] = [
      { login: 'low', score: 0.1 },
      { login: 'high', score: 0.9, rationale: 'owner' },
      { login: 'mid', score: 0.5 },
    ];
    const out = renderSuggestedReviewers(reviewers);
    const highIdx = out.indexOf('@high');
    const midIdx = out.indexOf('@mid');
    const lowIdx = out.indexOf('@low');
    expect(highIdx).toBeLessThan(midIdx);
    expect(midIdx).toBeLessThan(lowIdx);
    expect(out).toContain('(score 0.90)');
    expect(out).toContain('— owner');
  });

  it('honours explicit rank over score', () => {
    // Router says rank 1 / 2 / 3 and we display in that order, even if
    // score-order would disagree (e.g. tiebreak on login).
    const reviewers: SuggestedReviewer[] = [
      { login: 'third', score: 0.5, rank: 3 },
      { login: 'first', score: 0.5, rank: 1 },
      { login: 'second', score: 0.5, rank: 2 },
    ];
    const out = renderSuggestedReviewers(reviewers);
    const firstIdx = out.indexOf('@first');
    const secondIdx = out.indexOf('@second');
    const thirdIdx = out.indexOf('@third');
    expect(firstIdx).toBeLessThan(secondIdx);
    expect(secondIdx).toBeLessThan(thirdIdx);
  });

  it('puts ranked entries ahead of unranked in a mixed payload', () => {
    const reviewers: SuggestedReviewer[] = [
      { login: 'unranked', score: 0.99 },
      { login: 'ranked', score: 0.1, rank: 1 },
    ];
    const out = renderSuggestedReviewers(reviewers);
    expect(out.indexOf('@ranked')).toBeLessThan(out.indexOf('@unranked'));
  });

  it('renders teams distinctly from individual users', () => {
    const reviewers: SuggestedReviewer[] = [
      { login: 'acme/billing', score: 0.5, is_team: true, rationale: 'CODEOWNERS match' },
      { login: 'alice', score: 0.4, rationale: 'authored' },
    ];
    const out = renderSuggestedReviewers(reviewers);
    // Team rendered inside a code span with a "(team)" marker so GitHub
    // doesn't @-ping the team by default — keeps the opt-in for the PR
    // author rather than making Mnemos noisy.
    expect(out).toContain('`@acme/billing` (team)');
    // Plain user still gets a bare @-mention.
    expect(out).toMatch(/- @alice/);
  });

  it('omits the section for an empty list', () => {
    expect(renderSuggestedReviewers([])).toBe('');
  });
});

describe('footer', () => {
  it('stamps the Mnemos version when provided', () => {
    const out = formatReviewComment({ mnemos_version: '0.1.0-alpha.0' });
    expect(out).toContain('Reviewed by Mnemos 0.1.0-alpha.0');
  });

  it('falls back to a plain "Reviewed by Mnemos" when version is missing', () => {
    const out = formatReviewComment({});
    expect(out).toContain('Reviewed by Mnemos');
    // No stray version marker.
    expect(out).not.toMatch(/Reviewed by Mnemos\s+v?\d/);
  });

  it('honours an issues_url override', () => {
    const out = formatReviewComment({
      issues_url: 'https://github.com/acme/mnemos-fork/issues/new',
    });
    expect(out).toContain('https://github.com/acme/mnemos-fork/issues/new');
  });

  it('uses the default issues URL when not overridden', () => {
    const out = formatReviewComment({});
    expect(out).toContain('/mnemos/issues/new/choose');
  });
});
