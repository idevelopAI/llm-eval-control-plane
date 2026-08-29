// @vitest-environment node

import { describe, expect, it } from 'vitest';

import { releaseDecision } from '../test/release-evidence';
import { HostedBoundaryError } from './hosted-boundary-error';
import {
  hostedReadErrorResponse,
  hostedReadSuccessResponse,
} from './hosted-read-response';

function headerEntries(response: Response): Record<string, string> {
  return Object.fromEntries(response.headers.entries());
}

describe('hosted read responses', () => {
  it('creates a fresh private success response with only safe metadata', async () => {
    const response = hostedReadSuccessResponse({
      data: releaseDecision,
      requestId: 'request-safe_01',
    });

    expect(response.status).toBe(200);
    expect(headerEntries(response)).toEqual({
      'cache-control': 'private, no-store, max-age=0',
      'content-type': 'application/json; charset=utf-8',
      'cross-origin-resource-policy': 'same-origin',
      'referrer-policy': 'no-referrer',
      'x-content-type-options': 'nosniff',
      'x-request-id': 'request-safe_01',
    });
    await expect(response.json()).resolves.toEqual(releaseDecision);
  });

  it('drops an unsafe success request identifier', () => {
    const response = hostedReadSuccessResponse({
      data: releaseDecision,
      requestId: 'private request id',
    });

    expect(response.headers.has('x-request-id')).toBe(false);
    expect(JSON.stringify(headerEntries(response))).not.toContain(
      'private request id',
    );
  });

  it.each([
    ['authentication_required', 401],
    ['invalid_request', 400],
    ['method_not_allowed', 405],
    ['permission_denied', 403],
    ['request_not_allowed', 403],
    ['resource_not_found', 404],
    ['service_configuration_invalid', 503],
    ['unexpected_upstream_response', 502],
    ['upstream_unavailable', 503],
  ] as const)('serializes %s as a bounded error document', async (code, status) => {
    const error = new HostedBoundaryError(code);
    const response = hostedReadErrorResponse(error, 'request-safe_02');

    expect(response.status).toBe(status);
    expect(headerEntries(response)).toMatchObject({
      'cache-control': 'private, no-store, max-age=0',
      'content-type': 'application/json; charset=utf-8',
      'cross-origin-resource-policy': 'same-origin',
      'referrer-policy': 'no-referrer',
      'x-content-type-options': 'nosniff',
      'x-request-id': 'request-safe_02',
    });
    expect(await response.json()).toEqual({
      error: {
        code,
        details: [],
        message: error.message,
        request_id: 'request-safe_02',
      },
      schema_version: 'api-error/v1',
    });
  });

  it('preserves only the fixed method allowlist header', () => {
    const response = hostedReadErrorResponse(
      new HostedBoundaryError('method_not_allowed'),
      'request-safe_03',
    );

    expect(response.headers.get('allow')).toBe('GET');
    expect(response.headers.has('set-cookie')).toBe(false);
    expect(response.headers.has('www-authenticate')).toBe(false);
    expect(response.headers.has('location')).toBe(false);
    expect(response.headers.has('access-control-allow-origin')).toBe(false);
  });

  it('collapses unknown errors and unsafe request identifiers', async () => {
    const privateSentinel = 'private exception and header detail';
    const response = hostedReadErrorResponse(
      new Error(privateSentinel),
      privateSentinel,
    );
    const serialized = `${JSON.stringify(headerEntries(response))}${await response.text()}`;

    expect(response.status).toBe(500);
    expect(serialized).not.toContain(privateSentinel);
    expect(serialized).toContain('internal_error');
    expect(response.headers.get('x-request-id')).toBe('hreq_unavailable');
  });
});
