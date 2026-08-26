import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ControlPlaneApiError,
  createControlPlaneClient,
} from './client';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('createControlPlaneClient', () => {
  it('injects runtime credentials without returning them in application data', async () => {
    const fetchMock = vi.fn(async (request: Request) => {
      void request;
      return new Response(
        JSON.stringify({
          items: [],
          next_cursor: null,
          schema_version: 'release-decision-page/v1',
        }),
        {
          headers: {
            'content-type': 'application/json',
            'x-request-id': 'request_test_001',
          },
          status: 200,
        },
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));
    const result = await client.listReleaseDecisions({
      limit: 10,
      status: 'failed',
    });

    const request = fetchMock.mock.calls[0]?.[0] as unknown as Request;
    expect(request.headers.get('authorization')).toBe(
      'Bearer memory-only-test-value',
    );
    expect(request.headers.get('x-project-id')).toBe('project-test');
    expect(request.cache).toBe('no-store');
    expect(request.url).toContain('/v1/release-decisions');
    expect(request.url).toContain('status=failed');
    expect(result.requestId).toBe('request_test_001');
    expect(JSON.stringify(result)).not.toContain('memory-only-test-value');
  });

  it('fails safely before a request when the runtime credential is absent', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const client = createControlPlaneClient(() => null);

    await expect(client.listReleaseDecisions()).rejects.toMatchObject({
      code: 'authentication_required',
      status: 401,
    } satisfies Partial<ControlPlaneApiError>);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('discards raw network failures from the surfaced error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('transport details that must not reach the interface');
      }),
    );
    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));

    const error = await client.listReleaseDecisions().catch((value) => value);
    expect(error).toBeInstanceOf(ControlPlaneApiError);
    expect(error).toMatchObject({
      code: 'network_error',
      message: 'The control plane could not be reached.',
      status: 0,
    });
    expect(String(error)).not.toContain('transport details');
  });
});
