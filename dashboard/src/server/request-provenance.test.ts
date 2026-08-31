// @vitest-environment node

import { describe, expect, it } from 'vitest';

import { HostedBoundaryError } from './hosted-boundary-error';
import {
  createHostedControlPlaneConfiguration,
  type HostedControlPlaneConfiguration,
} from './hosted-config';
import { PLATFORM_USER_ID_HEADER } from './platform-identity';
import { requireHostedReadProvenance } from './request-provenance';

const OWNER_ID = 'opaque-owner:01/site';
const SITE_ORIGIN = 'https://dashboard.example.com';

function configuration(): HostedControlPlaneConfiguration {
  return createHostedControlPlaneConfiguration({
    ownerUserId: OWNER_ID,
    projectId: 'public-example-project_01',
    readToken: ['cpk_', 'A'.repeat(43)].join(''),
    siteOrigin: SITE_ORIGIN,
    upstreamOrigin: 'https://control-plane.example.com',
  });
}

function validHeaders(): Headers {
  return new Headers({
    [PLATFORM_USER_ID_HEADER]: OWNER_ID,
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'same-origin',
    'Sec-Fetch-Site': 'same-origin',
  });
}

function request({
  headers = validHeaders(),
  method = 'GET',
  url = `${SITE_ORIGIN}/api/control-plane/release-decisions`,
}: {
  headers?: Headers;
  method?: string;
  url?: string;
} = {}): Request {
  return new Request(url, { headers, method });
}

function boundaryError(
  action: () => void,
  code: HostedBoundaryError['code'],
  status: number,
) {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(HostedBoundaryError);
    expect(error).toMatchObject({ code, status });
    return error as HostedBoundaryError;
  }
  throw new Error('Expected a hosted boundary error.');
}

describe('hosted read provenance', () => {
  it('accepts an authenticated same-origin fetch without an Origin header', () => {
    expect(
      requireHostedReadProvenance(request(), configuration()),
    ).toBeUndefined();
  });

  it('accepts the exact canonical Origin when the browser sends it', () => {
    const headers = validHeaders();
    headers.set('Origin', SITE_ORIGIN);

    expect(
      requireHostedReadProvenance(request({ headers }), configuration()),
    ).toBeUndefined();
  });

  it('ignores platform cookies and forwarding headers', () => {
    const headers = validHeaders();
    headers.set('Cookie', 'platform_session=private-sentinel');
    headers.set('Forwarded', 'host=attacker.example');
    headers.set('X-Forwarded-Host', 'attacker.example');
    headers.set('X-Forwarded-Proto', 'http');

    expect(
      requireHostedReadProvenance(request({ headers }), configuration()),
    ).toBeUndefined();
  });

  it.each(['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']) (
    'rejects %s with an exact GET allowlist',
    (method) => {
      const error = boundaryError(
        () =>
          requireHostedReadProvenance(
            request({ method }),
            configuration(),
          ),
        'method_not_allowed',
        405,
      );

      expect(error.responseHeaders).toEqual({ Allow: 'GET' });
    },
  );

  it.each([
    ['HTTP URL', { url: 'http://dashboard.example.com/api/control-plane' }],
    ['wrong host', { url: 'https://attacker.example/api/control-plane' }],
    ['wrong port', { url: 'https://dashboard.example.com:8443/api/control-plane' }],
    ['cross-site fetch', { header: 'Sec-Fetch-Site', value: 'cross-site' }],
    ['same-site fetch', { header: 'Sec-Fetch-Site', value: 'same-site' }],
    ['navigation', { header: 'Sec-Fetch-Mode', value: 'navigate' }],
    ['CORS fetch', { header: 'Sec-Fetch-Mode', value: 'cors' }],
    ['no-CORS fetch', { header: 'Sec-Fetch-Mode', value: 'no-cors' }],
    ['document destination', { header: 'Sec-Fetch-Dest', value: 'document' }],
    ['null Origin', { header: 'Origin', value: 'null' }],
    ['wrong Origin', { header: 'Origin', value: 'https://attacker.example' }],
    [
      'comma-joined Origin',
      { header: 'Origin', value: `${SITE_ORIGIN}, https://attacker.example` },
    ],
    ['browser authorization', { header: 'Authorization', value: 'Bearer private-sentinel' }],
    ['browser project', { header: 'X-Project-ID', value: 'private-project' }],
  ] as const)('rejects %s with one content-free provenance error', (_label, change) => {
    const headers = validHeaders();
    if ('header' in change) headers.set(change.header, change.value);
    const changedRequest = request({
      headers,
      url: 'url' in change ? change.url : undefined,
    });

    const error = boundaryError(
      () => requireHostedReadProvenance(changedRequest, configuration()),
      'request_not_allowed',
      403,
    );
    expect(JSON.stringify(error)).not.toContain('private-sentinel');
    expect(JSON.stringify(error)).not.toContain('attacker.example');
  });

  it.each(['Sec-Fetch-Site', 'Sec-Fetch-Mode', 'Sec-Fetch-Dest']) (
    'rejects a missing %s header',
    (name) => {
      const headers = validHeaders();
      headers.delete(name);

      boundaryError(
        () =>
          requireHostedReadProvenance(
            request({ headers }),
            configuration(),
          ),
        'request_not_allowed',
        403,
      );
    },
  );

  it('preserves platform authentication and owner authorization failures', () => {
    const missingIdentity = validHeaders();
    missingIdentity.delete(PLATFORM_USER_ID_HEADER);
    boundaryError(
      () =>
        requireHostedReadProvenance(
          request({ headers: missingIdentity }),
          configuration(),
        ),
      'authentication_required',
      401,
    );

    const wrongIdentity = validHeaders();
    wrongIdentity.set(PLATFORM_USER_ID_HEADER, 'opaque-other-user');
    boundaryError(
      () =>
        requireHostedReadProvenance(
          request({ headers: wrongIdentity }),
          configuration(),
        ),
      'permission_denied',
      403,
    );
  });

  it('converts header-reader failures without exposing the cause', () => {
    const privateCause = 'private header reader detail';
    const malformedRequest = {
      headers: {
        get() {
          throw new Error(privateCause);
        },
      },
      method: 'GET',
      url: `${SITE_ORIGIN}/api/control-plane/release-decisions`,
    };

    const error = boundaryError(
      () =>
        requireHostedReadProvenance(
          malformedRequest,
          configuration(),
        ),
      'request_not_allowed',
      403,
    );
    expect(`${error.message}${error.stack}`).not.toContain(privateCause);
  });
});
