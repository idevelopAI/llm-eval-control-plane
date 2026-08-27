import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ControlPlaneApiError,
  createControlPlaneClient,
} from './client';

const observedStatistics = {
  maximum: 1,
  mean: 1,
  minimum: 1,
  p50: 1,
  p95: 1,
  sample_count: 1,
  small_sample: true,
  suppressed: false,
};
const unavailableStatistics = {
  maximum: null,
  mean: null,
  minimum: null,
  p50: null,
  p95: null,
  sample_count: 0,
  small_sample: true,
  suppressed: false,
};

function validDistributions() {
  const scoreValues = {
    attempted: 1,
    errors: 0,
    scored: 1,
    skipped: 0,
    statistics: observedStatistics,
  };
  const measurement = {
    attempted: 1,
    measured: 0,
    statistics: unavailableStatistics,
    target_failures: 1,
    unavailable: 1,
  };
  const run = (role: 'baseline' | 'candidate') => ({
    execution_mode: 'offline_mock',
    input_units: measurement,
    latency_ms: measurement,
    output_units: measurement,
    role,
    run_id: `run-${role}`,
    simulated: true,
    total_units: measurement,
  });
  return {
    baseline: run('baseline'),
    candidate: run('candidate'),
    decision_id: 'decision-001',
    schema_version: 'release-decision-distributions/v1',
    score: {
      baseline: scoreValues,
      candidate: scoreValues,
      delta: {
        attempted: 1,
        compared: 1,
        incomparable: 0,
        statistics: observedStatistics,
      },
      gate_slice: 'priority',
      metric: 'quality.exact_match',
    },
  };
}

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
    expect(request.credentials).toBe('same-origin');
    expect(request.mode).toBe('same-origin');
    expect(request.redirect).toBe('error');
    expect(request.referrerPolicy).toBe('no-referrer');
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

  it('requests only typed redacted case and distribution projections', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            decision_id: 'decision-001',
            items: [],
            next_cursor: null,
            schema_version: 'release-decision-case-page/v1',
          }),
          { headers: { 'content-type': 'application/json' }, status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify(validDistributions()),
          { headers: { 'content-type': 'application/json' }, status: 200 },
        ),
      );
    vi.stubGlobal('fetch', fetchMock);
    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));

    await client.listReleaseDecisionCases('decision-001', {
      case_slice: 'language/de',
      change: 'newly_failing',
      gate_slice: 'priority',
      limit: 25,
      metric: 'quality.exact_match',
    });
    await client.getReleaseDecisionDistributions('decision-001', {
      gate_slice: 'priority',
      metric: 'quality.exact_match',
    });

    const caseRequest = fetchMock.mock.calls[0]?.[0] as unknown as Request;
    const distributionRequest = fetchMock.mock.calls[1]?.[0] as unknown as Request;
    expect(caseRequest.url).toContain('/decision-001/cases?');
    expect(caseRequest.url).toContain('case_slice=language%2Fde');
    expect(caseRequest.url).toContain('change=newly_failing');
    expect(distributionRequest.url).toContain('/decision-001/distributions?');
    expect(distributionRequest.url).toContain('metric=quality.exact_match');
    expect(distributionRequest.headers.get('authorization')).toBe(
      'Bearer memory-only-test-value',
    );
  });

  it('drops invalid request identifiers from success and error results', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [],
            next_cursor: null,
            schema_version: 'release-decision-page/v1',
          }),
          {
            headers: {
              'content-type': 'application/json',
              'x-request-id': 'invalid request id',
            },
            status: 200,
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: {
              code: 'resource_not_found',
              details: [],
              message: 'Not found',
              request_id: 'invalid body request id',
            },
            schema_version: 'api-error/v1',
          }),
          { headers: { 'content-type': 'application/json' }, status: 404 },
        ),
      );
    vi.stubGlobal('fetch', fetchMock);
    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));

    await expect(client.listReleaseDecisions()).resolves.toMatchObject({
      requestId: null,
    });
    await expect(client.listReleaseDecisions()).rejects.toMatchObject({
      requestId: null,
      status: 404,
    });
  });

  it('preserves cancellation instead of reporting a network failure', async () => {
    const abortError = new DOMException('The operation was aborted.', 'AbortError');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw abortError;
      }),
    );
    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));

    await expect(client.listReleaseDecisions()).rejects.toMatchObject({
      name: 'AbortError',
      message: 'The request was canceled.',
    });
  });

  it('rejects malformed success payloads instead of trusting static types', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            items: [],
            next_cursor: null,
            raw_output: 'private-success-sentinel',
            schema_version: 'release-decision-page/v1',
          }),
          { headers: { 'content-type': 'application/json' }, status: 200 },
        ),
      ),
    );
    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));

    const error = await client.listReleaseDecisions().catch((value) => value);
    expect(error).toMatchObject({
      code: 'unexpected_response',
      message: 'The control plane returned an unexpected response.',
    });
    expect(JSON.stringify(error)).not.toContain('private-success-sentinel');
  });

  it('never surfaces server error text or details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            error: {
              code: 'private_error_code',
              details: [{ location: ['private'], type: 'private-detail-sentinel' }],
              message: 'private-message-sentinel',
              request_id: 'request_safe_001',
            },
            schema_version: 'api-error/v1',
          }),
          { headers: { 'content-type': 'application/json' }, status: 500 },
        ),
      ),
    );
    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));

    const error = await client.listReleaseDecisions().catch((value) => value);
    expect(error).toMatchObject({
      code: 'unexpected_response',
      message: 'The control plane could not complete this request.',
      requestId: 'request_safe_001',
      status: 500,
    });
    expect(JSON.stringify(error)).not.toContain('private');
  });

  it('sanitizes credential-source failures before any request', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const client = createControlPlaneClient(() => {
      throw new Error('private-vault-sentinel');
    });

    const error = await client.listReleaseDecisions().catch((value) => value);
    expect(error).toMatchObject({
      code: 'authentication_required',
      message: 'A read-only control-plane session is required.',
      status: 401,
    });
    expect(String(error)).not.toContain('private-vault-sentinel');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('normalizes custom abort reasons to a safe cancellation', async () => {
    const controller = new AbortController();
    controller.abort(new Error('private-abort-sentinel'));
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw controller.signal.reason;
      }),
    );
    const client = createControlPlaneClient(() => ({
      accessToken: 'memory-only-test-value',
      projectId: 'project-test',
    }));

    const error = await client
      .listReleaseDecisions({}, controller.signal)
      .catch((value) => value);
    expect(error).toMatchObject({
      name: 'AbortError',
      message: 'The request was canceled.',
    });
    expect(String(error)).not.toContain('private-abort-sentinel');
  });
});
