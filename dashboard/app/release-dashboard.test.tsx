import axe from 'axe-core';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  releaseCases,
  releaseDecision,
  releaseDecisionPage,
  releaseDistributions,
} from '@/src/test/release-evidence';
import ReleaseDashboard from './release-dashboard';

const TEST_TOKEN = `cpk_${'A'.repeat(43)}`;

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    headers: {
      'content-type': 'application/json',
      'x-request-id': 'request_live_001',
    },
    status,
  });
}

function liveFetch() {
  return vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === '/v1/release-decisions') {
      return jsonResponse(releaseDecisionPage);
    }
    if (url.pathname.endsWith('/cases')) return jsonResponse(releaseCases);
    if (url.pathname.endsWith('/distributions')) {
      return jsonResponse(releaseDistributions);
    }
    if (url.pathname === '/v1/release-decisions/decision-001') {
      return jsonResponse(releaseDecision);
    }
    return jsonResponse(
      {
        error: {
          code: 'resource_not_found',
          details: [],
          message: 'Not found',
          request_id: 'request_live_001',
        },
        schema_version: 'api-error/v1',
      },
      404,
    );
  });
}

async function enterLiveMode(user: ReturnType<typeof userEvent.setup>) {
  const liveButton = screen.getByRole('button', { name: 'Use local live data' });
  await waitFor(() => expect((liveButton as HTMLButtonElement).disabled).toBe(false));
  await user.click(liveButton);
}

async function submitCredential(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Project ID'), 'project-alpha');
  await user.type(screen.getByLabelText('Read-only access token'), TEST_TOKEN);
  await user.click(
    screen.getByRole('button', { name: 'Connect and load newest decision' }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ReleaseDashboard', () => {
  it('never renders bearer entry on a hosted origin', () => {
    vi.stubGlobal('location', {
      hostname: 'dashboard.example',
      protocol: 'https:',
    });
    vi.stubGlobal('fetch', vi.fn());
    render(<ReleaseDashboard />);

    expect(
      (screen.getByRole('button', {
        name: 'Use local live data',
      }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.queryByLabelText('Read-only access token')).toBeNull();
    expect(
      screen.getByText(
        'Browser bearer entry is disabled outside an HTTP loopback origin.',
      ),
    ).toBeTruthy();
  });

  it('starts in an explicit zero-request fixture mode', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    expect(
      screen.getByText('Immutable fixture · no live request made'),
    ).toBeTruthy();
    await waitFor(() =>
      expect(
        (screen.getByRole('button', {
          name: 'Use local live data',
        }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('loads the newest live decision through bounded redacted reads', async () => {
    const user = userEvent.setup();
    const fetchMock = liveFetch();
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem');
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    expect(
      screen.queryByRole('heading', { name: 'Release blocked' }),
    ).toBeNull();
    await submitCredential(user);

    await screen.findByRole('heading', { name: 'Release blocked' });
    expect(screen.getByText('project-alpha')).toBeTruthy();
    expect(
      screen.getByText(
        'Live API · redacted response contracts · credential held in memory',
      ),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain(TEST_TOKEN);
    expect(storageWrite).not.toHaveBeenCalled();

    const requests = fetchMock.mock.calls.map(
      (call) => call[0] as unknown as Request,
    );
    const listRequest = requests.find(
      (request) => new URL(request.url).pathname === '/v1/release-decisions',
    );
    expect(listRequest?.url).toContain('limit=20');
    expect(listRequest?.url).toContain('order=desc');
    expect(listRequest?.headers.get('authorization')).toBe(`Bearer ${TEST_TOKEN}`);
    const caseRequest = requests.find((request) => request.url.includes('/cases?'));
    expect(caseRequest?.url).toContain('limit=100');
    expect(caseRequest?.url).toContain('gate_slice=language%2Fde');
    expect(caseRequest?.url).not.toContain('change=');
    expect(
      requests.find((request) => request.url.includes('/distributions?')),
    ).toBeTruthy();
  });

  it('filters case transitions without refetching aggregate distributions', async () => {
    const user = userEvent.setup();
    const fetchMock = liveFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    const filter = screen.getByLabelText('Case transition');
    expect((filter as unknown as { value: string }).value).toBe('all');

    await user.selectOptions(filter, 'newly_failing');
    await waitFor(() =>
      expect((filter as unknown as { value: string }).value).toBe(
        'newly_failing',
      ),
    );

    const requests = fetchMock.mock.calls.map(
      (call) => new URL((call[0] as unknown as Request).url),
    );
    const caseRequests = requests.filter((url) => url.pathname.endsWith('/cases'));
    expect(caseRequests).toHaveLength(2);
    expect(caseRequests[1].searchParams.get('change')).toBe('newly_failing');
    expect(
      requests.filter((url) => url.pathname.endsWith('/distributions')),
    ).toHaveLength(1);
  });

  it('loads bounded case pages without repeating distribution reads', async () => {
    const user = userEvent.setup();
    const twoAggregate = {
      ...releaseDecision.aggregates[0],
      baseline: {
        ...releaseDecision.aggregates[0].baseline,
        attempted: 2,
        scored: 2,
      },
      candidate: {
        ...releaseDecision.aggregates[0].candidate,
        attempted: 2,
        scored: 2,
      },
    };
    const decisionWithTwoCases = {
      ...releaseDecision,
      aggregates: [twoAggregate],
      gates: [{ ...releaseDecision.gates[0], aggregate: twoAggregate }],
    };
    const twoMeasurement = {
      ...releaseDistributions.baseline.latency_ms,
      attempted: 2,
      measured: 2,
      statistics: {
        ...releaseDistributions.baseline.latency_ms.statistics,
        sample_count: 2,
      },
    };
    const distributionsWithTwoCases = {
      ...releaseDistributions,
      baseline: {
        ...releaseDistributions.baseline,
        input_units: twoMeasurement,
        latency_ms: twoMeasurement,
        output_units: twoMeasurement,
        total_units: twoMeasurement,
      },
      candidate: {
        ...releaseDistributions.candidate,
        input_units: twoMeasurement,
        latency_ms: twoMeasurement,
        output_units: twoMeasurement,
        total_units: twoMeasurement,
      },
      score: {
        ...releaseDistributions.score,
        baseline: {
          ...releaseDistributions.score.baseline,
          attempted: 2,
          scored: 2,
          statistics: {
            ...releaseDistributions.score.baseline.statistics,
            sample_count: 2,
          },
        },
        candidate: {
          ...releaseDistributions.score.candidate,
          attempted: 2,
          scored: 2,
          statistics: {
            ...releaseDistributions.score.candidate.statistics,
            sample_count: 2,
          },
        },
        delta: {
          ...releaseDistributions.score.delta,
          attempted: 2,
          compared: 2,
          statistics: {
            ...releaseDistributions.score.delta.statistics,
            sample_count: 2,
          },
        },
      },
    };
    const secondPage = {
      ...releaseCases,
      items: [{ ...releaseCases.items[0], case_id: 'case-002' }],
      next_cursor: null,
    };
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/release-decisions') {
        return jsonResponse(releaseDecisionPage);
      }
      if (url.pathname === '/v1/release-decisions/decision-001') {
        return jsonResponse(decisionWithTwoCases);
      }
      if (url.pathname.endsWith('/cases')) {
        return jsonResponse(url.searchParams.has('cursor') ? secondPage : releaseCases);
      }
      if (url.pathname.endsWith('/distributions')) {
        return jsonResponse(distributionsWithTwoCases);
      }
      throw new Error('unexpected test request');
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await user.click(screen.getByRole('button', { name: 'Load more cases' }));

    await screen.findByRole('heading', { name: 'case-002' });
    expect(screen.getByText('2 shown · selected gate')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Load more cases' })).toBeNull();
    const requests = fetchMock.mock.calls.map(
      (call) => new URL((call[0] as unknown as Request).url),
    );
    const caseRequests = requests.filter((url) => url.pathname.endsWith('/cases'));
    expect(caseRequests).toHaveLength(2);
    expect(caseRequests[1].searchParams.get('cursor')).toBe(
      'bounded-next-page',
    );
    expect(
      requests.filter((url) => url.pathname.endsWith('/distributions')),
    ).toHaveLength(1);
  });

  it('rejects duplicate case IDs returned by a later page', async () => {
    const user = userEvent.setup();
    const fetchMock = liveFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await user.click(screen.getByRole('button', { name: 'Load more cases' }));

    await screen.findByText('Selected gate evidence could not be refreshed.');
    expect(screen.getByRole('heading', { name: 'case-001' })).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      'Release case pagination is inconsistent',
    );
  });

  it('fails closed when a filtered case projection contradicts its query', async () => {
    const user = userEvent.setup();
    const fetchMock = liveFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await user.selectOptions(
      screen.getByLabelText('Case transition'),
      'newly_passing',
    );

    await screen.findByText('Selected gate evidence could not be refreshed.');
    expect(screen.getByRole('heading', { name: 'Release blocked' })).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      'Release case projection is inconsistent',
    );
  });

  it('switches between the bounded newest decision history', async () => {
    const user = userEvent.setup();
    const olderDecision = {
      ...releaseDecision,
      created_at: '2026-08-26T10:15:00Z',
      decision_digest: `sha256:${'8'.repeat(64)}`,
      decision_id: 'decision-000',
    };
    const decisions = {
      ...releaseDecisionPage,
      items: [
        releaseDecisionPage.items[0],
        {
          ...releaseDecisionPage.items[0],
          created_at: olderDecision.created_at,
          decision_digest: olderDecision.decision_digest,
          decision_id: olderDecision.decision_id,
        },
      ],
      next_cursor: 'older-page-available',
    };
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/release-decisions') {
        return jsonResponse(decisions);
      }
      if (url.pathname === '/v1/release-decisions/decision-000') {
        return jsonResponse(olderDecision);
      }
      if (url.pathname === '/v1/release-decisions/decision-001') {
        return jsonResponse(releaseDecision);
      }
      if (url.pathname.endsWith('/cases')) {
        return jsonResponse(
          url.pathname.includes('decision-000')
            ? { ...releaseCases, decision_id: 'decision-000' }
            : releaseCases,
        );
      }
      if (url.pathname.endsWith('/distributions')) {
        return jsonResponse(
          url.pathname.includes('decision-000')
            ? { ...releaseDistributions, decision_id: 'decision-000' }
            : releaseDistributions,
        );
      }
      throw new Error('unexpected test request');
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    const picker = screen.getByLabelText('Decision history');
    const selectedValue = () =>
      (picker as unknown as { value: string }).value;
    expect(picker.querySelectorAll('option')).toHaveLength(2);
    expect(selectedValue()).toBe('decision-001');
    expect(
      screen.getByText('2 newest immutable decisions loaded · older decisions available through the API'),
    ).toBeTruthy();

    await user.selectOptions(picker, 'decision-000');
    await waitFor(() => expect(selectedValue()).toBe('decision-000'));
    expect(
      document.querySelector('.provenance-strip code')?.textContent,
    ).toBe('decision-000');
    const paths = fetchMock.mock.calls.map(
      (call) => new URL((call[0] as unknown as Request).url).pathname,
    );
    expect(paths).toContain('/v1/release-decisions/decision-000');
    expect(paths).toContain('/v1/release-decisions/decision-000/cases');
    expect(paths).toContain('/v1/release-decisions/decision-000/distributions');
  });

  it('rejects a duplicate decision list before requesting details', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        ...releaseDecisionPage,
        items: [
          releaseDecisionPage.items[0],
          releaseDecisionPage.items[0],
        ],
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);

    await screen.findByRole('heading', { name: 'Live evidence is unavailable' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('Decision history')).toBeNull();
  });

  it('aborts and ignores an in-flight response when disconnected', async () => {
    const user = userEvent.setup();
    const pending: {
      request?: Request;
      resolve?: (response: Response) => void;
    } = {};
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (value: Request) =>
          new Promise<Response>((resolve) => {
            pending.request = value;
            pending.resolve = resolve;
          }),
      ),
    );
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', {
      name: 'Loading bounded release evidence',
    });
    await user.click(
      screen.getByRole('button', {
        name: 'Disconnect and return to fixture',
      }),
    );

    expect(pending.request?.signal.aborted).toBe(true);
    pending.resolve?.(jsonResponse(releaseDecisionPage));
    await waitFor(() =>
      expect(
        screen.getByText('Immutable fixture · no live request made'),
      ).toBeTruthy(),
    );
    expect(
      screen.queryByText('Live API · redacted response contracts · credential held in memory'),
    ).toBeNull();
  });

  it('keeps a live empty result explicit without falling back to the fixture', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        items: [],
        next_cursor: null,
        schema_version: 'release-decision-page/v1',
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);

    await screen.findByRole('heading', { name: 'No release decisions yet' });
    expect(screen.queryByText('decision_regression_001')).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('omits gate_slice when a user selects the global gate', async () => {
    const user = userEvent.setup();
    const globalAggregate = {
      ...releaseDecision.gates[0].aggregate,
      slice: null,
    };
    const decisionWithGlobalGate = {
      ...releaseDecision,
      aggregates: [...releaseDecision.aggregates, globalAggregate],
      gates: [
        ...releaseDecision.gates,
        {
          ...releaseDecision.gates[0],
          aggregate: globalAggregate,
          failure_codes: [],
          regression_passed: true,
          slice: null,
          status: 'passed' as const,
          threshold_passed: true,
        },
      ],
    };
    const globalCases = {
      ...releaseCases,
      items: releaseCases.items.map((item) => ({ ...item, gate_slice: null })),
    };
    const globalDistributions = {
      ...releaseDistributions,
      score: { ...releaseDistributions.score, gate_slice: null },
    };
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/release-decisions') {
        return jsonResponse(releaseDecisionPage);
      }
      if (url.pathname === '/v1/release-decisions/decision-001') {
        return jsonResponse(decisionWithGlobalGate);
      }
      if (url.pathname.endsWith('/cases')) {
        return jsonResponse(
          url.searchParams.has('gate_slice') ? releaseCases : globalCases,
        );
      }
      if (url.pathname.endsWith('/distributions')) {
        return jsonResponse(
          url.searchParams.has('gate_slice')
            ? releaseDistributions
            : globalDistributions,
        );
      }
      throw new Error('unexpected test request');
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await user.click(
      screen.getByRole('button', {
        name: /^Exact matchquality\.exact_matchall cases/,
      }),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(6));

    const latestRequests = fetchMock.mock.calls
      .slice(-2)
      .map((call) => new URL((call[0] as unknown as Request).url));
    expect(latestRequests.every((url) => !url.searchParams.has('gate_slice'))).toBe(
      true,
    );
  });

  it('sanitizes a live failure and never renders fixture evidence', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: 'persistence_unavailable',
              details: [],
              message: 'private-server-message-sentinel',
              request_id: 'request_live_001',
            },
            schema_version: 'api-error/v1',
          },
          503,
        ),
      ),
    );
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);

    await screen.findByRole('heading', { name: 'Live evidence is unavailable' });
    expect(document.body.textContent).not.toContain('private-server-message-sentinel');
    expect(screen.queryByText('decision_regression_001')).toBeNull();
    expect(screen.getByText('Request ID: request_live_001')).toBeTruthy();
    expect(
      screen.getByRole('button', { name: 'Retry live request' }),
    ).toBeTruthy();
  });

  it('clears the volatile session after an authorization failure', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: 'authentication_required',
              details: [],
              message: 'Authentication credentials are invalid',
              request_id: 'request_live_001',
            },
            schema_version: 'api-error/v1',
          },
          401,
        ),
      ),
    );
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);

    await screen.findByRole('heading', { name: 'Live evidence is unavailable' });
    expect(screen.getByLabelText('Read-only access token')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry live request' })).toBeNull();
  });

  it('drops stale evidence and aborts sibling reads when authorization expires', async () => {
    const user = userEvent.setup();
    let caseReads = 0;
    let distributionReads = 0;
    const delayedDistribution: { request?: Request } = {};
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/release-decisions') {
        return jsonResponse(releaseDecisionPage);
      }
      if (url.pathname === '/v1/release-decisions/decision-001') {
        return jsonResponse(releaseDecision);
      }
      if (url.pathname.endsWith('/cases')) {
        caseReads += 1;
        if (caseReads === 1) return jsonResponse(releaseCases);
        return jsonResponse(
          {
            error: {
              code: 'authentication_required',
              details: [],
              message: 'expired-private-message',
              request_id: 'request_live_001',
            },
            schema_version: 'api-error/v1',
          },
          401,
        );
      }
      if (url.pathname.endsWith('/distributions')) {
        distributionReads += 1;
        if (distributionReads === 1) return jsonResponse(releaseDistributions);
        delayedDistribution.request = request;
        return new Promise<Response>(() => undefined);
      }
      throw new Error('unexpected test request');
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await user.click(
      screen.getByRole('button', {
        name: /^Exact match/,
      }),
    );

    await screen.findByRole('heading', { name: 'Live evidence is unavailable' });
    expect(screen.queryByRole('heading', { name: 'Release blocked' })).toBeNull();
    expect(screen.getByLabelText('Read-only access token')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry live request' })).toBeNull();
    expect(document.body.textContent).not.toContain('expired-private-message');
    expect(delayedDistribution.request?.signal.aborted).toBe(true);
  });

  it('rejects list and detail identity drift', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/release-decisions') {
        return jsonResponse(releaseDecisionPage);
      }
      return jsonResponse({
        ...releaseDecision,
        created_at: '2026-08-27T14:33:00Z',
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);

    await screen.findByRole('heading', { name: 'Live evidence is unavailable' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('heading', { name: 'Release blocked' })).toBeNull();
  });

  it('has no structural accessibility violations in local connect mode', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn());
    const { container } = render(<ReleaseDashboard />);
    await enterLiveMode(user);

    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it('has no structural accessibility violations in ready and expanded states', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', liveFetch());
    const { container } = render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    const readyResults = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(readyResults.violations).toEqual([]);

    await user.click(
      screen.getByRole('button', {
        name: 'Inspect scoring evidence for case-001',
      }),
    );
    const expandedResults = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(expandedResults.violations).toEqual([]);
  });

  it('has no structural accessibility violations in a live error state', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: 'persistence_unavailable',
              details: [],
              message: 'private-error-sentinel',
              request_id: 'request_live_001',
            },
            schema_version: 'api-error/v1',
          },
          503,
        ),
      ),
    );
    const { container } = render(<ReleaseDashboard />);

    await enterLiveMode(user);
    await submitCredential(user);
    await screen.findByRole('heading', { name: 'Live evidence is unavailable' });
    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });
});
