// @vitest-environment node

import { describe, expect, it, vi } from 'vitest';

import { releaseDecision } from '../test/release-evidence';
import { handleHostedDashboardRead } from './hosted-read-handler';
import {
  parseDecisionDetail,
  parseDecisionListQuery,
} from './dashboard-read-operation';
import {
  createHostedControlPlaneConfiguration,
  type HostedControlPlaneConfiguration,
} from './hosted-config';

const OWNER_ID = 'opaque-owner:01/site';
const PROJECT_ID = 'public-example-project_01';
const READ_TOKEN = ['cpk_', 'A'.repeat(43)].join('');
const SITE_ORIGIN = 'https://dashboard.example.com';

function configuration(): HostedControlPlaneConfiguration {
  return createHostedControlPlaneConfiguration({
    ownerUserId: OWNER_ID,
    projectId: PROJECT_ID,
    readToken: READ_TOKEN,
    siteOrigin: SITE_ORIGIN,
    upstreamOrigin: 'https://control-plane.example.com',
  });
}

function browserRequest({
  headers = {},
  method = 'GET',
  path = '/api/control-plane/release-decisions/decision-001',
}: {
  headers?: Record<string, string>;
  method?: string;
  path?: string;
} = {}): Request {
  return new Request(`${SITE_ORIGIN}${path}`, {
    headers: {
      'oai-authenticated-user-id': OWNER_ID,
      'sec-fetch-dest': 'empty',
      'sec-fetch-mode': 'same-origin',
      'sec-fetch-site': 'same-origin',
      ...headers,
    },
    method,
  });
}

function upstreamResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    headers: {
      'Content-Type': 'application/json',
      'Set-Cookie': 'private=upstream',
      'X-Request-ID': 'upstream-safe_01',
    },
    status,
  });
}

describe('hosted read handler', () => {
  it('composes identity, provenance, fixed execution, and a private response', async () => {
    const fetchMock = vi.fn(async () => upstreamResponse(releaseDecision));
    const resolver = vi.fn(() => parseDecisionDetail('decision-001'));

    const response = await handleHostedDashboardRead({
      configuration: configuration(),
      dependencies: { fetch: fetchMock as typeof fetch },
      request: browserRequest(),
      requestId: 'hosted-safe_01',
      resolveOperation: resolver,
    });

    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe(
      'private, no-store, max-age=0',
    );
    expect(response.headers.get('x-request-id')).toBe('upstream-safe_01');
    expect(response.headers.has('set-cookie')).toBe(false);
    await expect(response.json()).resolves.toEqual(releaseDecision);
    expect(resolver).toHaveBeenCalledOnce();

    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.toString()).toBe(
      'https://control-plane.example.com/v1/release-decisions/decision-001',
    );
    expect([...(init.headers as Headers).entries()]).toEqual([
      ['accept', 'application/json'],
      ['authorization', `Bearer ${READ_TOKEN}`],
      ['x-project-id', PROJECT_ID],
    ]);
  });

  it.each([
    ['missing identity', { 'oai-authenticated-user-id': '' }, 401],
    ['wrong owner', { 'oai-authenticated-user-id': 'opaque-other' }, 403],
    ['cross-site fetch', { 'sec-fetch-site': 'cross-site' }, 403],
    ['browser authorization', { authorization: 'Bearer private-browser' }, 403],
    ['browser project assertion', { 'x-project-id': 'private-browser' }, 403],
  ] as const)('rejects %s before resolving or fetching', async (_label, headers, status) => {
    const fetchMock = vi.fn();
    const resolver = vi.fn(() => parseDecisionDetail('decision-001'));
    const response = await handleHostedDashboardRead({
      configuration: configuration(),
      dependencies: { fetch: fetchMock as typeof fetch },
      request: browserRequest({ headers }),
      requestId: 'hosted-safe_02',
      resolveOperation: resolver,
    });

    expect(response.status).toBe(status);
    expect(response.headers.get('cache-control')).toContain('no-store');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(resolver).not.toHaveBeenCalled();
  });

  it.each(['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])(
    'denies %s explicitly before any operation',
    async (method) => {
      const fetchMock = vi.fn();
      const resolver = vi.fn(() => parseDecisionDetail('decision-001'));
      const response = await handleHostedDashboardRead({
        configuration: configuration(),
        dependencies: { fetch: fetchMock as typeof fetch },
        request: browserRequest({ method }),
        requestId: 'hosted-safe_03',
        resolveOperation: resolver,
      });

      expect(response.status).toBe(405);
      expect(response.headers.get('allow')).toBe('GET');
      expect(fetchMock).not.toHaveBeenCalled();
      expect(resolver).not.toHaveBeenCalled();
    },
  );

  it('sanitizes an invalid allowlisted operation before fetch', async () => {
    const fetchMock = vi.fn();
    const privateSentinel = 'private-query-detail';
    const response = await handleHostedDashboardRead({
      configuration: configuration(),
      dependencies: { fetch: fetchMock as typeof fetch },
      request: browserRequest({
        path: `/api/control-plane/release-decisions?${privateSentinel}=1`,
      }),
      requestId: 'hosted-safe_04',
      resolveOperation: (url) => parseDecisionListQuery(url.searchParams),
    });
    const serialized = await response.text();

    expect(response.status).toBe(400);
    expect(serialized).not.toContain(privateSentinel);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sanitizes unknown resolver failures as internal errors', async () => {
    const privateSentinel = 'private resolver failure';
    const response = await handleHostedDashboardRead({
      configuration: configuration(),
      dependencies: { fetch: vi.fn() as typeof fetch },
      request: browserRequest(),
      requestId: privateSentinel,
      resolveOperation: () => {
        throw new Error(privateSentinel);
      },
    });
    const serialized = `${JSON.stringify(Object.fromEntries(response.headers))}${await response.text()}`;

    expect(response.status).toBe(500);
    expect(serialized).not.toContain(privateSentinel);
    expect(serialized).toContain('internal_error');
  });

  it('discards an upstream authorization failure and its metadata', async () => {
    const privateSentinel = 'private upstream authorization detail';
    const fetchMock = vi.fn(async () =>
      upstreamResponse({ detail: privateSentinel }, 401),
    );
    const response = await handleHostedDashboardRead({
      configuration: configuration(),
      dependencies: { fetch: fetchMock as typeof fetch },
      request: browserRequest(),
      requestId: 'hosted-safe_05',
      resolveOperation: () => parseDecisionDetail('decision-001'),
    });
    const serialized = `${JSON.stringify(Object.fromEntries(response.headers))}${await response.text()}`;

    expect(response.status).toBe(503);
    expect(serialized).not.toContain(privateSentinel);
    expect(response.headers.has('set-cookie')).toBe(false);
  });
});
