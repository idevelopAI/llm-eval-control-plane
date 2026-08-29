// @vitest-environment node

import { describe, expect, it, vi } from 'vitest';

import {
  executeDashboardRead,
  HOSTED_READ_TIMEOUT_MS,
  type DashboardReadDependencies,
} from './dashboard-read-executor';
import {
  parseDecisionCases,
  parseDecisionDetail,
  parseDecisionDistributions,
  parseDecisionListQuery,
} from './dashboard-read-operation';
import { HostedBoundaryError } from './hosted-boundary-error';
import {
  createHostedControlPlaneConfiguration,
  type HostedControlPlaneConfiguration,
} from './hosted-config';
import {
  releaseCases,
  releaseDecision,
  releaseDecisionPage,
  releaseDistributions,
} from '../test/release-evidence';

const PROJECT_ID = 'portfolio-project_01';
const READ_TOKEN = ['cpk_', 'A'.repeat(43)].join('');

function configuration(): HostedControlPlaneConfiguration {
  return createHostedControlPlaneConfiguration({
    ownerUserId: 'opaque-owner:01/site',
    projectId: PROJECT_ID,
    readToken: READ_TOKEN,
    siteOrigin: 'https://dashboard.portfolio.dev',
    upstreamOrigin: 'https://control-plane.portfolio.dev',
  });
}

function jsonResponse(
  payload: unknown,
  { headers = {}, status = 200 }: { headers?: Record<string, string>; status?: number } = {},
): Response {
  return new Response(JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json', ...headers },
    status,
  });
}

function dependencies(response: Response): {
  dependencies: DashboardReadDependencies;
  fetchMock: ReturnType<typeof vi.fn>;
} {
  const fetchMock = vi.fn(async () => response);
  return {
    dependencies: { fetch: fetchMock as typeof fetch },
    fetchMock,
  };
}

async function boundaryError(
  action: () => Promise<unknown>,
  code: HostedBoundaryError['code'],
  status: number,
) {
  try {
    await action();
  } catch (error) {
    expect(error).toBeInstanceOf(HostedBoundaryError);
    expect(error).toMatchObject({ code, status });
    return error as HostedBoundaryError;
  }
  throw new Error('Expected a dashboard read error.');
}

describe('hosted dashboard read executor', () => {
  it('sends only fixed server headers and supported Worker fetch options', async () => {
    const { dependencies: injected, fetchMock } = dependencies(
      jsonResponse(releaseDecisionPage, { headers: { 'X-Request-ID': 'request-01' } }),
    );
    const operation = parseDecisionListQuery(
      new URLSearchParams({ limit: '10', order: 'desc' }),
    );

    await expect(
      executeDashboardRead(operation, configuration(), injected),
    ).resolves.toEqual({ data: releaseDecisionPage, requestId: 'request-01' });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.toString()).toBe(
      'https://control-plane.portfolio.dev/v1/release-decisions?limit=10&order=desc',
    );
    expect(init).toMatchObject({
      cache: 'no-store',
      method: 'GET',
      redirect: 'error',
    });
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect([...(init.headers as Headers).entries()]).toEqual([
      ['accept', 'application/json'],
      ['authorization', `Bearer ${READ_TOKEN}`],
      ['x-project-id', PROJECT_ID],
    ]);
  });

  it.each([
    [
      'detail',
      parseDecisionDetail('decision-001'),
      releaseDecision,
    ],
    [
      'cases',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          case_slice: 'language/de',
          change: 'newly_failing',
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      releaseCases,
    ],
    [
      'distributions',
      parseDecisionDistributions(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      releaseDistributions,
    ],
  ] as const)('validates the %s projection and request consistency', async (_label, operation, payload) => {
    const { dependencies: injected } = dependencies(jsonResponse(payload));

    await expect(
      executeDashboardRead(operation, configuration(), injected),
    ).resolves.toEqual({ data: payload, requestId: null });
  });

  it('drops an unsafe upstream request ID', async () => {
    const { dependencies: injected } = dependencies(
      jsonResponse(releaseDecision, {
        headers: { 'X-Request-ID': 'private request id' },
      }),
    );

    await expect(
      executeDashboardRead(
        parseDecisionDetail('decision-001'),
        configuration(),
        injected,
      ),
    ).resolves.toEqual({ data: releaseDecision, requestId: null });
  });

  it.each([401, 403, 409, 429, 500, 503])(
    'sanitizes upstream status %s as service unavailability',
    async (status) => {
      const privateSentinel = 'private-upstream-body';
      const { dependencies: injected } = dependencies(
        jsonResponse({ detail: privateSentinel }, {
          headers: {
            Location: 'https://private.example',
            'Set-Cookie': 'private=cookie',
            'WWW-Authenticate': 'Bearer private-realm',
          },
          status,
        }),
      );

      const error = await boundaryError(
        () =>
          executeDashboardRead(
            parseDecisionDetail('decision-001'),
            configuration(),
            injected,
          ),
        'upstream_unavailable',
        503,
      );
      expect(`${error.message}${JSON.stringify(error)}${error.stack}`).not.toContain(
        privateSentinel,
      );
      expect(JSON.stringify(error)).not.toContain('private.example');
      expect(JSON.stringify(error)).not.toContain('private-realm');
    },
  );

  it('maps a missing resource without reading or forwarding its body', async () => {
    const { dependencies: injected } = dependencies(
      jsonResponse({ detail: 'private-not-found-detail' }, { status: 404 }),
    );

    const error = await boundaryError(
      () =>
        executeDashboardRead(
          parseDecisionDetail('decision-001'),
          configuration(),
          injected,
        ),
      'resource_not_found',
      404,
    );
    expect(JSON.stringify(error)).not.toContain('private-not-found-detail');
  });

  it('treats a list 404 as deployment skew instead of a missing resource', async () => {
    const { dependencies: injected } = dependencies(
      jsonResponse({ detail: 'private-route-detail' }, { status: 404 }),
    );

    const error = await boundaryError(
      () =>
        executeDashboardRead(
          parseDecisionListQuery(new URLSearchParams()),
          configuration(),
          injected,
        ),
      'upstream_unavailable',
      503,
    );
    expect(JSON.stringify(error)).not.toContain('private-route-detail');
  });

  it('rejects an upstream redirect even when a response is otherwise valid', async () => {
    const response = jsonResponse(releaseDecision);
    Object.defineProperty(response, 'redirected', { value: true });
    const { dependencies: injected } = dependencies(response);

    await boundaryError(
      () =>
        executeDashboardRead(
          parseDecisionDetail('decision-001'),
          configuration(),
          injected,
        ),
      'upstream_unavailable',
      503,
    );
  });

  it('sanitizes a fetch exception and does not retain its cause', async () => {
    const privateCause = 'private network endpoint detail';
    const fetchMock = vi.fn(async () => {
      throw new Error(privateCause);
    });

    const error = await boundaryError(
      () =>
        executeDashboardRead(
          parseDecisionDetail('decision-001'),
          configuration(),
          { fetch: fetchMock as typeof fetch },
        ),
      'upstream_unavailable',
      503,
    );
    expect(`${error.message}${JSON.stringify(error)}${error.stack}`).not.toContain(
      privateCause,
    );
  });

  it('aborts and sanitizes a hung upstream fetch at the fixed deadline', async () => {
    vi.useFakeTimers();
    try {
      const privateCause = 'private timed-out endpoint';
      let observedSignal: AbortSignal | undefined;
      const fetchMock = vi.fn(
        async (_input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            observedSignal = init?.signal ?? undefined;
            observedSignal?.addEventListener(
              'abort',
              () => reject(new Error(privateCause)),
              { once: true },
            );
          }),
      );

      const pending = executeDashboardRead(
        parseDecisionDetail('decision-001'),
        configuration(),
        { fetch: fetchMock as typeof fetch },
      );
      const errorPromise = boundaryError(
        () => pending,
        'upstream_unavailable',
        503,
      );
      await vi.advanceTimersByTimeAsync(HOSTED_READ_TIMEOUT_MS);
      const error = await errorPromise;

      expect(observedSignal?.aborted).toBe(true);
      expect(`${error.message}${JSON.stringify(error)}${error.stack}`).not.toContain(
        privateCause,
      );
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('bounds a stalled response body with the same fixed deadline', async () => {
    vi.useFakeTimers();
    try {
      const privateCause = 'private stalled stream detail';
      let observedSignal: AbortSignal | undefined;
      const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        observedSignal = init?.signal ?? undefined;
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            observedSignal?.addEventListener(
              'abort',
              () => controller.error(new Error(privateCause)),
              { once: true },
            );
          },
        });
        return new Response(body, {
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const pending = executeDashboardRead(
        parseDecisionDetail('decision-001'),
        configuration(),
        { fetch: fetchMock as typeof fetch },
      );
      const errorPromise = boundaryError(
        () => pending,
        'upstream_unavailable',
        503,
      );
      await vi.advanceTimersByTimeAsync(HOSTED_READ_TIMEOUT_MS);
      const error = await errorPromise;

      expect(observedSignal?.aborted).toBe(true);
      expect(`${error.message}${JSON.stringify(error)}${error.stack}`).not.toContain(
        privateCause,
      );
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('propagates caller cancellation without exposing its reason', async () => {
    const controller = new AbortController();
    const privateCause = 'private browser cancellation reason';
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            'abort',
            () => reject(new Error(privateCause)),
            { once: true },
          );
        }),
    );

    const pending = executeDashboardRead(
      parseDecisionDetail('decision-001'),
      configuration(),
      { fetch: fetchMock as typeof fetch },
      controller.signal,
    );
    controller.abort(privateCause);
    const error = await boundaryError(
      () => pending,
      'upstream_unavailable',
      503,
    );
    expect(`${error.message}${JSON.stringify(error)}${error.stack}`).not.toContain(
      privateCause,
    );
  });

  it('clears the upstream deadline after a successful read', async () => {
    vi.useFakeTimers();
    try {
      const { dependencies: injected } = dependencies(
        jsonResponse(releaseDecision),
      );

      await executeDashboardRead(
        parseDecisionDetail('decision-001'),
        configuration(),
        injected,
      );
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    [
      'requested list status',
      parseDecisionListQuery(new URLSearchParams({ status: 'passed' })),
      releaseDecisionPage,
    ],
    [
      'list limit',
      parseDecisionListQuery(new URLSearchParams({ limit: '1' })),
      {
        ...releaseDecisionPage,
        items: [releaseDecisionPage.items[0], releaseDecisionPage.items[0]],
      },
    ],
    [
      'list cursor shape',
      parseDecisionListQuery(new URLSearchParams()),
      { ...releaseDecisionPage, next_cursor: 'private cursor' },
    ],
    [
      'descending list order',
      parseDecisionListQuery(new URLSearchParams({ order: 'desc' })),
      {
        ...releaseDecisionPage,
        items: [
          { ...releaseDecisionPage.items[0], created_at: '2026-08-27T14:31:00Z' },
          { ...releaseDecisionPage.items[0], created_at: '2026-08-27T14:32:00Z' },
        ],
      },
    ],
    [
      'case decision ID',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      { ...releaseCases, decision_id: 'decision-other' },
    ],
    [
      'case metric',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      {
        ...releaseCases,
        items: [{ ...releaseCases.items[0], metric: 'private.metric' }],
      },
    ],
    [
      'case gate slice',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/fr',
          metric: 'quality.exact_match',
        }),
      ),
      releaseCases,
    ],
    [
      'case slice',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          case_slice: 'language/fr',
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      releaseCases,
    ],
    [
      'case change',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          change: 'newly_passing',
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      releaseCases,
    ],
    [
      'case limit',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/de',
          limit: '1',
          metric: 'quality.exact_match',
        }),
      ),
      {
        ...releaseCases,
        items: [releaseCases.items[0], releaseCases.items[0]],
      },
    ],
    [
      'case cursor shape',
      parseDecisionCases(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      { ...releaseCases, next_cursor: 'private cursor' },
    ],
    [
      'distribution decision ID',
      parseDecisionDistributions(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      { ...releaseDistributions, decision_id: 'decision-other' },
    ],
    [
      'distribution metric',
      parseDecisionDistributions(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/de',
          metric: 'quality.exact_match',
        }),
      ),
      {
        ...releaseDistributions,
        score: { ...releaseDistributions.score, metric: 'private.metric' },
      },
    ],
    [
      'distribution gate slice',
      parseDecisionDistributions(
        'decision-001',
        new URLSearchParams({
          gate_slice: 'language/fr',
          metric: 'quality.exact_match',
        }),
      ),
      releaseDistributions,
    ],
  ] as const)(
    'rejects a response inconsistent with the %s filter',
    async (_label, operation, payload) => {
      const { dependencies: injected } = dependencies(jsonResponse(payload));
      await boundaryError(
        () => executeDashboardRead(operation, configuration(), injected),
        'unexpected_upstream_response',
        502,
      );
    },
  );

  it.each([
    ['unknown response field', { ...releaseDecision, private_field: 'sentinel' }],
    ['wrong detail ID', { ...releaseDecision, decision_id: 'decision-other' }],
  ] as const)('rejects %s as an unexpected projection', async (_label, payload) => {
    const operation = parseDecisionDetail('decision-001');
    const { dependencies: injected } = dependencies(jsonResponse(payload));

    const error = await boundaryError(
      () => executeDashboardRead(operation, configuration(), injected),
      'unexpected_upstream_response',
      502,
    );
    expect(JSON.stringify(error)).not.toContain('private.metric');
    expect(JSON.stringify(error)).not.toContain('sentinel');
  });
});
