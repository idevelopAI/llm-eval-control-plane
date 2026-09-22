import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ReleaseDecision } from '@/src/api/client';
import { pairKey } from '@/src/api/suite-history-validation';
import {
  suitePage,
  suiteRunPage,
  suiteTargetPage,
  suitePairPage,
} from '@/src/test/suite-history';
import {
  releaseDecision,
  releaseDecisionPage,
  releaseCases,
  releaseDistributions,
} from '@/src/test/release-evidence';
import {
  historicalDecision,
  historicalDecisionPage,
  historicalCases,
  historicalDistributions,
} from '@/src/test/historical-release';
import ReleaseDashboard from './release-dashboard';

const TEST_TOKEN = `cpk_${'A'.repeat(43)}`;
const historicalPath = `/v1/release-decisions/${historicalDecision.decision_id}`;
const reviewName = `Review gates for ${historicalDecision.decision_id}`;

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    headers: { 'content-type': 'application/json' },
    status,
  });
}

function failure(status: number) {
  return json(
    {
      schema_version: 'api-error/v1',
      error: {
        code: status === 403 ? 'permission_denied' : 'resource_not_found',
        details: [],
        message: 'private-history-error-sentinel',
        request_id: 'request_history_001',
      },
    },
    status,
  );
}

function harness(
  override?: (request: Request) => Promise<Response> | undefined,
) {
  const fetch = vi.fn(async (request: Request) => {
    const overridden = override?.(request);
    if (overridden) return overridden;
    const path = new URL(request.url).pathname;
    const data: Record<string, unknown> = {
      '/v1/release-decisions': releaseDecisionPage,
      '/v1/release-decisions/decision-001': releaseDecision,
      '/v1/release-decisions/decision-001/cases': releaseCases,
      '/v1/release-decisions/decision-001/distributions':
        releaseDistributions,
      '/v1/suites': suitePage,
      '/v1/suite-runs': suiteRunPage,
      '/v1/suite-comparisons': historicalDecisionPage,
      '/v1/suite-targets': suiteTargetPage,
      '/v1/suite-target-pairs': suitePairPage,
      [historicalPath]: historicalDecision,
      [`${historicalPath}/cases`]: historicalCases,
      [`${historicalPath}/distributions`]: historicalDistributions,
    };
    if (!(path in data)) throw new Error('Unexpected test request');
    return json(data[path]);
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

async function connect(user: ReturnType<typeof userEvent.setup>) {
  const live = screen.getByRole('button', { name: 'Use local live data' });
  await waitFor(() =>
    expect((live as HTMLButtonElement).disabled).toBe(false),
  );
  await user.click(live);
  await user.type(screen.getByLabelText('Project ID'), 'project-alpha');
  await user.type(
    screen.getByLabelText('Read-only access token'),
    TEST_TOKEN,
  );
  await user.click(
    screen.getByRole('button', { name: 'Connect and load newest decision' }),
  );
  await screen.findByRole('button', { name: 'Browse suite history' });
}

async function browse(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    screen.getByRole('button', { name: 'Browse suite history' }),
  );
  await screen.findByRole('button', { name: reviewName });
}

function selectedDecision() {
  return (
    screen.getByLabelText('Decision history') as unknown as { value: string }
  ).value;
}

afterEach(() => vi.unstubAllGlobals());

describe('historical gate review', () => {
  it.each([false, true])(
    'verifies the selected target pair before loading detail evidence (mismatch=%s)',
    async (mismatch) => {
      const pair = {
        ...suitePairPage.items[0],
        baseline_target: historicalDecision.baseline,
        candidate_target: mismatch
          ? {
              ...historicalDecision.candidate,
              digest: `sha256:${'0'.repeat(64)}`,
            }
          : historicalDecision.candidate,
      };
      const fetch = harness((request) =>
        new URL(request.url).pathname === '/v1/suite-target-pairs'
          ? Promise.resolve(json({ ...suitePairPage, items: [pair] }))
          : undefined,
      );
      const user = userEvent.setup();
      render(<ReleaseDashboard />);
      await connect(user);
      await browse(user);
      await user.selectOptions(
        screen.getByLabelText('Decision target pair'),
        pairKey(pair),
      );
      await screen.findByRole('button', { name: reviewName });
      await user.click(screen.getByRole('button', { name: reviewName }));
      if (mismatch) {
        await screen.findByRole('alert');
        expect(selectedDecision()).toBe(releaseDecision.decision_id);
        expect(
          fetch.mock.calls.some(([request]) =>
            new URL(request.url).pathname.startsWith(`${historicalPath}/`),
          ),
        ).toBe(false);
      } else {
        await waitFor(() =>
          expect(selectedDecision()).toBe(historicalDecision.decision_id),
        );
        expect(
          fetch.mock.calls.some(
            ([request]) =>
              new URL(request.url).pathname === `${historicalPath}/cases`,
          ),
        ).toBe(true);
      }
    },
  );

  it('opens a decision outside the recent page, focuses review, and returns to recent evidence', async () => {
    const fetch = harness();
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem');
    const user = userEvent.setup();
    render(<ReleaseDashboard />);
    await connect(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await browse(user);
    expect(
      fetch.mock.calls.some(
        ([request]) => new URL(request.url).pathname === historicalPath,
      ),
    ).toBe(false);
    await user.selectOptions(
      screen.getByLabelText('Case transition'),
      'newly_failing',
    );
    await waitFor(() =>
      expect(
        (
          screen.getByLabelText('Case transition') as unknown as {
            value: string;
          }
        ).value,
      ).toBe('newly_failing'),
    );
    await user.click(screen.getByRole('button', { name: reviewName }));
    await waitFor(() =>
      expect(selectedDecision()).toBe(historicalDecision.decision_id),
    );
    expect(document.activeElement?.id).toBe('overview');
    expect(
      screen
        .getByRole('button', { name: reviewName })
        .getAttribute('aria-current'),
    ).toBe('true');
    expect(
      screen.getByText(
        'Selected from suite history, outside the newest collection.',
      ),
    ).not.toBeNull();
    expect(
      screen.getByLabelText('Decision history').querySelectorAll('option'),
    ).toHaveLength(2);
    expect(
      document.querySelector('.provenance-strip code')?.textContent,
    ).toBe(historicalDecision.decision_id);
    const historyRequests = fetch.mock.calls
      .map(([request]) => request)
      .filter((request) =>
        new URL(request.url).pathname.startsWith(historicalPath),
      );
    expect(historyRequests).toHaveLength(3);
    for (const request of historyRequests) {
      expect(request.method).toBe('GET');
      expect(request.cache).toBe('no-store');
      expect(request.redirect).toBe('error');
      expect(request.url).not.toContain(TEST_TOKEN);
    }
    const cases = historyRequests.find((request) =>
      new URL(request.url).pathname.endsWith('/cases'),
    );
    expect(cases?.url).toContain('limit=100');
    expect(cases?.url).toContain('gate_slice=language%2Fde');
    expect(cases?.url).not.toContain('change=');
    expect(
      (
        screen.getByLabelText('Case transition') as unknown as {
          value: string;
        }
      ).value,
    ).toBe('all');
    expect(storageWrite).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(TEST_TOKEN);
    await user.selectOptions(
      screen.getByLabelText('Decision history'),
      'decision-001',
    );
    await waitFor(() => expect(selectedDecision()).toBe('decision-001'));
    expect(
      screen.getByLabelText('Decision history').querySelectorAll('option'),
    ).toHaveLength(1);
    expect(
      screen.queryByText(
        'Selected from suite history, outside the newest collection.',
      ),
    ).toBeNull();
    expect(
      fetch.mock.calls.filter(
        ([request]) =>
          new URL(request.url).pathname === '/v1/release-decisions',
      ),
    ).toHaveLength(1);
  });

  it.each([
    ['missing suite', { suite: undefined }],
    [
      'suite name',
      { suite: { ...historicalDecision.suite!, name: 'different-suite' } },
    ],
    [
      'suite revision',
      { suite: { ...historicalDecision.suite!, revision: 99 } },
    ],
    [
      'suite digest',
      {
        suite: {
          ...historicalDecision.suite!,
          digest: `sha256:${'0'.repeat(64)}`,
        },
      },
    ],
    ['decision digest', { decision_digest: `sha256:${'0'.repeat(64)}` }],
    ['baseline run', { baseline_run_id: 'wrong-baseline' }],
    ['candidate run', { candidate_run_id: 'wrong-candidate' }],
    ['creation time', { created_at: '2026-08-25T12:20:00Z' }],
  ] satisfies [string, Partial<ReleaseDecision>][])(
    'rejects a mismatched %s before fetching case evidence',
    async (_label, changes) => {
      const fetch = harness((request) =>
        new URL(request.url).pathname === historicalPath
          ? Promise.resolve(json({ ...historicalDecision, ...changes }))
          : undefined,
      );
      const user = userEvent.setup();
      render(<ReleaseDashboard />);
      await connect(user);
      await screen.findByRole('heading', { name: 'Release blocked' });
      await browse(user);
      await user.click(screen.getByRole('button', { name: reviewName }));
      expect((await screen.findByRole('alert')).textContent).toContain(
        'Selected decision could not be opened',
      );
      expect(selectedDecision()).toBe('decision-001');
      expect(
        fetch.mock.calls.filter(([request]) =>
          new URL(request.url).pathname.startsWith(`${historicalPath}/`),
        ),
      ).toHaveLength(0);
      expect(document.body.textContent).not.toContain(
        'Historical decision identity mismatch',
      );
    },
  );

  it('preserves verified evidence on a missing decision and recovers on an explicit retry', async () => {
    let attempts = 0;
    harness((request) =>
      new URL(request.url).pathname === historicalPath && attempts++ === 0
        ? Promise.resolve(failure(404))
        : undefined,
    );
    const user = userEvent.setup();
    render(<ReleaseDashboard />);
    await connect(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await browse(user);
    await user.click(screen.getByRole('button', { name: reviewName }));
    await screen.findByRole('alert');
    expect(selectedDecision()).toBe('decision-001');
    expect(document.body.textContent).not.toContain(
      'private-history-error-sentinel',
    );
    await user.click(screen.getByRole('button', { name: reviewName }));
    await waitFor(() =>
      expect(selectedDecision()).toBe(historicalDecision.decision_id),
    );
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('can review history even when the recent collection is empty', async () => {
    harness((request) =>
      new URL(request.url).pathname === '/v1/release-decisions'
        ? Promise.resolve(json({ ...releaseDecisionPage, items: [] }))
        : undefined,
    );
    const user = userEvent.setup();
    render(<ReleaseDashboard />);
    await connect(user);
    await screen.findByRole('heading', { name: 'No release decisions yet' });
    await browse(user);
    await user.click(screen.getByRole('button', { name: reviewName }));
    await screen.findByRole('heading', { name: 'Release blocked' });
    expect(selectedDecision()).toBe(historicalDecision.decision_id);
    expect(
      screen.getByLabelText('Decision history').querySelectorAll('option'),
    ).toHaveLength(1);
  });

  it('clears the entire session if history detail access is revoked', async () => {
    harness((request) =>
      new URL(request.url).pathname === historicalPath
        ? Promise.resolve(failure(403))
        : undefined,
    );
    const user = userEvent.setup();
    render(<ReleaseDashboard />);
    await connect(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await browse(user);
    await user.click(screen.getByRole('button', { name: reviewName }));
    await screen.findByLabelText('Read-only access token');
    expect(
      screen.queryByRole('heading', { name: 'Release blocked' }),
    ).toBeNull();
    expect(
      screen.queryByRole('heading', { name: 'Suite history' }),
    ).toBeNull();
    expect(document.body.textContent).not.toContain(
      'private-history-error-sentinel',
    );
  });

  it('aborts an opening decision on disconnect and ignores its late response', async () => {
    let pending:
      | { request: Request; resolve: (response: Response) => void }
      | undefined;
    const fetch = harness((request) =>
      new URL(request.url).pathname === historicalPath
        ? new Promise((resolve) => {
            pending = { request, resolve };
          })
        : undefined,
    );
    const user = userEvent.setup();
    render(<ReleaseDashboard />);
    await connect(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await browse(user);
    await user.click(screen.getByRole('button', { name: reviewName }));
    await screen.findByText(/Opening historical decision/);
    await user.click(
      screen.getByRole('button', {
        name: 'Disconnect and return to fixture',
      }),
    );
    expect(pending?.request.signal.aborted).toBe(true);
    await act(async () => pending?.resolve(json(historicalDecision)));
    expect(
      screen.queryByRole('heading', { name: 'Suite history' }),
    ).toBeNull();
    expect(
      fetch.mock.calls.some(([request]) =>
        new URL(request.url).pathname.startsWith(`${historicalPath}/`),
      ),
    ).toBe(false);
  });

  it('keeps the latest history selection when an earlier detail read completes late', async () => {
    const olderId = 'suite-decision-000';
    const olderPath = `/v1/release-decisions/${olderId}`;
    let pending:
      | { request: Request; resolve: (response: Response) => void }
      | undefined;
    const fetch = harness((request) => {
      const path = new URL(request.url).pathname;
      if (path === '/v1/suite-comparisons')
        return Promise.resolve(
          json({
            ...historicalDecisionPage,
            items: [
              historicalDecisionPage.items[0],
              { ...historicalDecisionPage.items[0], decision_id: olderId },
            ],
          }),
        );
      if (path === historicalPath)
        return new Promise((resolve) => {
          pending = { request, resolve };
        });
      if (path === olderPath)
        return Promise.resolve(
          json({ ...historicalDecision, decision_id: olderId }),
        );
      if (path === `${olderPath}/cases`)
        return Promise.resolve(
          json({ ...historicalCases, decision_id: olderId }),
        );
      if (path === `${olderPath}/distributions`)
        return Promise.resolve(
          json({ ...historicalDistributions, decision_id: olderId }),
        );
      return undefined;
    });
    const user = userEvent.setup();
    render(<ReleaseDashboard />);
    await connect(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await browse(user);
    await user.click(screen.getByRole('button', { name: reviewName }));
    await user.click(
      screen.getByRole('button', { name: `Review gates for ${olderId}` }),
    );
    await waitFor(() => expect(selectedDecision()).toBe(olderId));
    expect(pending?.request.signal.aborted).toBe(true);
    await act(async () => pending?.resolve(json(historicalDecision)));
    expect(selectedDecision()).toBe(olderId);
    expect(
      fetch.mock.calls.some(([request]) =>
        new URL(request.url).pathname.startsWith(`${historicalPath}/`),
      ),
    ).toBe(false);
    expect(
      screen.getByLabelText('Decision history').querySelectorAll('option'),
    ).toHaveLength(2);
  });

  it('clears history immediately on evidence authorization failure while its sibling stalls', async () => {
    let sibling: Request | undefined;
    harness((request) => {
      const path = new URL(request.url).pathname;
      if (path === `${historicalPath}/cases`)
        return Promise.resolve(failure(403));
      if (path === `${historicalPath}/distributions`) {
        sibling = request;
        return new Promise(() => undefined);
      }
      return undefined;
    });
    const user = userEvent.setup();
    render(<ReleaseDashboard />);
    await connect(user);
    await screen.findByRole('heading', { name: 'Release blocked' });
    await browse(user);
    await user.click(screen.getByRole('button', { name: reviewName }));
    await screen.findByLabelText('Read-only access token');
    expect(sibling?.signal.aborted).toBe(true);
    expect(
      screen.queryByRole('heading', { name: 'Suite history' }),
    ).toBeNull();
    expect(screen.queryByLabelText('Decision history')).toBeNull();
  });
});
