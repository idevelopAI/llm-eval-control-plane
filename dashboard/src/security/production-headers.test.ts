import { describe, expect, it } from 'vitest';

import nextConfig from '../../next.config';
import {
  PRIVATE_RESPONSE_HEADERS,
  PRODUCTION_SECURITY_HEADERS,
} from './production-headers';

function headerMap(headers: readonly { key: string; value: string }[]) {
  return new Map(headers.map(({ key, value }) => [key.toLowerCase(), value]));
}

describe('production response headers', () => {
  it('confines scripts, connections, forms, and framing to the Site', () => {
    const headers = headerMap(PRODUCTION_SECURITY_HEADERS);
    const policy = headers.get('content-security-policy');

    expect(policy).toContain("default-src 'self'");
    expect(policy).toContain("connect-src 'self'");
    expect(policy).toContain("form-action 'self'");
    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).toContain("object-src 'none'");
    expect(policy).toContain('upgrade-insecure-requests');
    expect(policy).not.toContain("'unsafe-eval'");
    expect(policy).not.toContain('https:');
    expect(policy).not.toContain('*');
  });

  it('sets transport, isolation, referrer, capability, and sniffing defenses', () => {
    expect(headerMap(PRODUCTION_SECURITY_HEADERS)).toEqual(
      new Map([
        [
          'content-security-policy',
          expect.any(String),
        ],
        ['cross-origin-opener-policy', 'same-origin'],
        ['cross-origin-resource-policy', 'same-origin'],
        [
          'permissions-policy',
          'browsing-topics=(), camera=(), geolocation=(), microphone=(), payment=(), usb=()',
        ],
        ['referrer-policy', 'no-referrer'],
        [
          'strict-transport-security',
          'max-age=63072000; includeSubDomains; preload',
        ],
        ['x-content-type-options', 'nosniff'],
        ['x-frame-options', 'DENY'],
      ]),
    );
  });

  it('marks document and API responses private and non-cacheable', () => {
    expect(headerMap(PRIVATE_RESPONSE_HEADERS)).toEqual(
      new Map([['cache-control', 'private, no-store, max-age=0']]),
    );
  });

  it('wires the defenses into every route and adds no-store at private surfaces', async () => {
    expect(nextConfig.headers).toBeTypeOf('function');

    const routes = await nextConfig.headers?.();

    expect(routes).toEqual([
      {
        headers: [...PRODUCTION_SECURITY_HEADERS],
        source: '/:path*',
      },
      {
        headers: [...PRODUCTION_SECURITY_HEADERS, ...PRIVATE_RESPONSE_HEADERS],
        source: '/',
      },
      {
        headers: [...PRODUCTION_SECURITY_HEADERS, ...PRIVATE_RESPONSE_HEADERS],
        source: '/api/:path*',
      },
    ]);
  });
});
