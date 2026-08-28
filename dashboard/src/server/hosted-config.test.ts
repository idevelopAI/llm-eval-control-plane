import { describe, expect, it } from 'vitest';

import { HostedBoundaryError } from './hosted-boundary-error';
import { createHostedControlPlaneConfiguration } from './hosted-config';
import { PLATFORM_USER_ID_HEADER } from './platform-identity';

const OWNER_ID = 'opaque-owner:01/site';
const PROJECT_ID = 'portfolio-project_01';
const READ_TOKEN = ['cpk_', 'A'.repeat(43)].join('');
const SITE_ORIGIN = 'https://dashboard.portfolio.dev';
const UPSTREAM_ORIGIN = 'https://control-plane.portfolio.dev';

function validInput() {
  return {
    ownerUserId: OWNER_ID,
    projectId: PROJECT_ID,
    readToken: READ_TOKEN,
    siteOrigin: SITE_ORIGIN,
    upstreamOrigin: UPSTREAM_ORIGIN,
  };
}

function expectConfigurationError(action: () => unknown, sentinel?: string) {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(HostedBoundaryError);
    expect(error).toMatchObject({
      code: 'service_configuration_invalid',
      message: 'Hosted live evidence is unavailable.',
      status: 503,
    });
    if (sentinel) {
      const rendered = `${String(error)}\n${JSON.stringify(error)}\n${
        (error as Error).stack ?? ''
      }`;
      expect(rendered).not.toContain(sentinel);
    }
    return;
  }
  throw new Error('Expected a hosted configuration error.');
}

describe('hosted control-plane configuration', () => {
  it('retains secrets in private fields and creates fresh minimal headers', () => {
    const configuration = createHostedControlPlaneConfiguration(validInput());
    const first = configuration.createUpstreamHeaders();
    const second = configuration.createUpstreamHeaders();

    expect(first).not.toBe(second);
    expect([...first.entries()]).toEqual([
      ['accept', 'application/json'],
      ['authorization', `Bearer ${READ_TOKEN}`],
      ['x-project-id', PROJECT_ID],
    ]);
    expect(configuration.siteOrigin()).toBe(SITE_ORIGIN);
    expect(configuration.upstreamOrigin()).toBe(UPSTREAM_ORIGIN);
    expect(Object.isFrozen(configuration)).toBe(true);
    expect(Object.keys(configuration)).toEqual([]);
    expect(String(configuration)).toBe('HostedControlPlaneConfiguration()');
    expect(JSON.stringify(configuration)).toBe('{"configured":true}');
    expect(JSON.stringify(configuration)).not.toContain(READ_TOKEN);
    expect(JSON.stringify(configuration)).not.toContain(PROJECT_ID);
    expect(JSON.stringify(configuration)).not.toContain(OWNER_ID);
  });

  it('normalizes a canonical trailing slash without accepting a path', () => {
    const configuration = createHostedControlPlaneConfiguration({
      ...validInput(),
      siteOrigin: `${SITE_ORIGIN}/`,
      upstreamOrigin: `${UPSTREAM_ORIGIN}/`,
    });

    expect(configuration.siteOrigin()).toBe(SITE_ORIGIN);
    expect(configuration.upstreamOrigin()).toBe(UPSTREAM_ORIGIN);
  });

  it('delegates owner authorization to the platform identity boundary', () => {
    const configuration = createHostedControlPlaneConfiguration(validInput());
    const headers = new Headers({ [PLATFORM_USER_ID_HEADER]: OWNER_ID });

    expect(configuration.authorizeOwner(headers)).toBeUndefined();
  });

  it.each([
    ['missing input', { projectId: undefined }],
    ['coerced project', { projectId: 42 }],
    ['padded project', { projectId: ` ${PROJECT_ID}` }],
    ['oversized project', { projectId: 'p'.repeat(129) }],
    ['wrong token prefix', { readToken: `key_${'A'.repeat(43)}` }],
    ['short token', { readToken: ['cpk_', 'A'.repeat(42)].join('') }],
    ['padded token', { readToken: ` ${READ_TOKEN}` }],
    ['invalid owner', { ownerUserId: `${OWNER_ID},other` }],
    ['HTTP Site', { siteOrigin: 'http://dashboard.portfolio.dev' }],
    ['Site credentials', { siteOrigin: 'https://user@dashboard.portfolio.dev' }],
    ['Site path', { siteOrigin: `${SITE_ORIGIN}/api` }],
    ['Site query', { siteOrigin: `${SITE_ORIGIN}?private=value` }],
    ['Site fragment', { siteOrigin: `${SITE_ORIGIN}#private` }],
    ['Site port', { siteOrigin: 'https://dashboard.portfolio.dev:8443' }],
    ['loopback', { upstreamOrigin: 'https://127.0.0.1' }],
    ['localhost', { upstreamOrigin: 'https://localhost' }],
    ['internal host', { upstreamOrigin: 'https://api.service.internal' }],
    ['single-label host', { upstreamOrigin: 'https://control-plane' }],
    ['non-ASCII host', { upstreamOrigin: 'https://éxample.dev' }],
    ['recursive origin', { upstreamOrigin: SITE_ORIGIN }],
  ] as const)('fails closed for %s', (_label, override) => {
    const sentinel = Object.values(override)[0];

    expectConfigurationError(
      () =>
        createHostedControlPlaneConfiguration({
          ...validInput(),
          ...override,
        }),
      typeof sentinel === 'string' ? sentinel : undefined,
    );
  });
});
