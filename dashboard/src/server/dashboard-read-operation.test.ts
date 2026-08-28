// @vitest-environment node

import { describe, expect, it } from 'vitest';

import {
  buildDashboardReadUrl,
  parseDecisionCases,
  parseDecisionDetail,
  parseDecisionDistributions,
  parseDecisionListQuery,
  type DashboardReadOperation,
} from './dashboard-read-operation';
import { HostedBoundaryError } from './hosted-boundary-error';
import {
  createHostedControlPlaneConfiguration,
  type HostedControlPlaneConfiguration,
} from './hosted-config';

function configuration(): HostedControlPlaneConfiguration {
  return createHostedControlPlaneConfiguration({
    ownerUserId: 'opaque-owner:01/site',
    projectId: 'portfolio-project_01',
    readToken: ['cpk_', 'A'.repeat(43)].join(''),
    siteOrigin: 'https://dashboard.portfolio.dev',
    upstreamOrigin: 'https://control-plane.portfolio.dev',
  });
}

function expectInvalid(action: () => unknown): void {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(HostedBoundaryError);
    expect(error).toMatchObject({
      code: 'invalid_request',
      message: 'The hosted request is invalid.',
      status: 400,
    });
    return;
  }
  throw new Error('Expected an invalid hosted request.');
}

describe('dashboard read operations', () => {
  it('builds the exact decision-list URL in canonical query order', () => {
    const operation = parseDecisionListQuery(
      new URLSearchParams({
        cursor: 'opaque_cursor-01',
        limit: '25',
        order: 'desc',
        status: 'failed',
      }),
    );

    expect(buildDashboardReadUrl(configuration(), operation).toString()).toBe(
      'https://control-plane.portfolio.dev/v1/release-decisions?limit=25&cursor=opaque_cursor-01&status=failed&order=desc',
    );
    expect(Object.isFrozen(operation)).toBe(true);
    expect(Object.isFrozen(operation.query)).toBe(true);
  });

  it('builds the exact decision-detail URL without accepting a query', () => {
    const operation = parseDecisionDetail('decision:2026.08-01');

    expect(buildDashboardReadUrl(configuration(), operation).toString()).toBe(
      'https://control-plane.portfolio.dev/v1/release-decisions/decision%3A2026.08-01',
    );
    expectInvalid(() =>
      parseDecisionDetail('decision-01', new URLSearchParams('debug=true')),
    );
  });

  it('builds the exact redacted-case URL', () => {
    const operation = parseDecisionCases(
      'decision-01',
      new URLSearchParams({
        case_slice: 'locale:en-US',
        change: 'newly_failing',
        cursor: 'next_page-01',
        gate_slice: 'tier=critical',
        limit: '100',
        metric: 'quality/groundedness:v1',
      }),
    );

    expect(buildDashboardReadUrl(configuration(), operation).toString()).toBe(
      'https://control-plane.portfolio.dev/v1/release-decisions/decision-01/cases?metric=quality%2Fgroundedness%3Av1&limit=100&cursor=next_page-01&gate_slice=tier%3Dcritical&case_slice=locale%3Aen-US&change=newly_failing',
    );
  });

  it('builds the exact distribution URL', () => {
    const operation = parseDecisionDistributions(
      'decision-01',
      new URLSearchParams({
        gate_slice: 'tier=critical',
        metric: 'safety/refusal',
      }),
    );

    expect(buildDashboardReadUrl(configuration(), operation).toString()).toBe(
      'https://control-plane.portfolio.dev/v1/release-decisions/decision-01/distributions?metric=safety%2Frefusal&gate_slice=tier%3Dcritical',
    );
  });

  it('preserves omission and relies on upstream defaults', () => {
    const operation = parseDecisionListQuery(new URLSearchParams());

    expect(buildDashboardReadUrl(configuration(), operation).toString()).toBe(
      'https://control-plane.portfolio.dev/v1/release-decisions',
    );
  });

  it.each([
    'incomparable',
    'newly_failing',
    'newly_passing',
    'unchanged_failing',
    'unchanged_passing',
  ] as const)('accepts the documented %s case change', (change) => {
    const operation = parseDecisionCases(
      'decision-01',
      new URLSearchParams({ change, metric: 'quality' }),
    );

    expect(buildDashboardReadUrl(configuration(), operation).searchParams.get('change')).toBe(
      change,
    );
  });

  it.each([
    ['unknown key', () => parseDecisionListQuery(new URLSearchParams('debug=true'))],
    ['duplicate key', () => parseDecisionListQuery(new URLSearchParams('limit=1&limit=2'))],
    ['empty limit', () => parseDecisionListQuery(new URLSearchParams('limit='))],
    ['zero limit', () => parseDecisionListQuery(new URLSearchParams('limit=0'))],
    ['padded limit', () => parseDecisionListQuery(new URLSearchParams('limit=01'))],
    ['signed limit', () => parseDecisionListQuery(new URLSearchParams('limit=+1'))],
    ['large limit', () => parseDecisionListQuery(new URLSearchParams('limit=101'))],
    ['unknown status', () => parseDecisionListQuery(new URLSearchParams('status=blocked'))],
    ['unknown order', () => parseDecisionListQuery(new URLSearchParams('order=newest'))],
    ['empty cursor', () => parseDecisionListQuery(new URLSearchParams('cursor='))],
    ['non-base64url cursor', () => parseDecisionListQuery(new URLSearchParams('cursor=abc%25'))],
    ['oversized cursor', () => parseDecisionListQuery(new URLSearchParams({ cursor: 'a'.repeat(2049) }))],
    ['missing metric', () => parseDecisionCases('decision-01', new URLSearchParams())],
    ['duplicate metric', () => parseDecisionCases('decision-01', new URLSearchParams('metric=a&metric=b'))],
    ['unknown case change', () => parseDecisionCases('decision-01', new URLSearchParams('metric=a&change=changed'))],
    ['path-shaped decision ID', () => parseDecisionDetail('../private')],
    ['encoded slash decision ID', () => parseDecisionDetail('decision%2Fprivate')],
    ['absolute decision ID', () => parseDecisionDetail('https://attacker.example')],
    ['oversized decision ID', () => parseDecisionDetail('d'.repeat(129))],
    ['invalid metric', () => parseDecisionDistributions('decision-01', new URLSearchParams('metric=%2Fprivate'))],
    ['invalid slice', () => parseDecisionDistributions('decision-01', new URLSearchParams('metric=a&gate_slice=%25ZZ'))],
  ] as const)('rejects %s before URL construction', (_label, action) => {
    expectInvalid(action);
  });

  it('fails closed for an unrepresentable operation kind', () => {
    expectInvalid(() =>
      buildDashboardReadUrl(configuration(), {
        kind: 'generic-proxy',
        path: 'https://attacker.example/private',
      } as unknown as DashboardReadOperation),
    );
  });
});
