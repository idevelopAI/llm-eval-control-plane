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
    expect(
      requests.find((request) => request.url.includes('/distributions?')),
    ).toBeTruthy();
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
});
