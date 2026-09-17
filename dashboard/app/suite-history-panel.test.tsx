import axe from 'axe-core';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import {
  createControlPlaneClient,
  ControlPlaneApiError,
} from '@/src/api/client';
import {
  suitePage,
  suiteRunPage,
  suiteDecisionPage,
} from '@/src/test/suite-history';
import { SuiteHistoryPanel } from './suite-history-panel';

function harness() {
  const client = createControlPlaneClient(() => null);
  vi.spyOn(client, 'listSuites').mockResolvedValue({
    data: suitePage,
    requestId: null,
  });
  vi.spyOn(client, 'listSuiteRuns').mockResolvedValue({
    data: suiteRunPage,
    requestId: null,
  });
  vi.spyOn(client, 'listSuiteDecisions').mockResolvedValue({
    data: suiteDecisionPage,
    requestId: null,
  });
  const onAuthenticationFailure = vi.fn();
  return { client, onAuthenticationFailure };
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    screen.getByRole('button', { name: 'Browse suite history' }),
  );
  await screen.findByRole('heading', { name: 'Evaluation runs' });
}

describe('local suite history panel', () => {
  it('loads only on explicit action, renders redacted metadata, and is accessible', async () => {
    const props = harness();
    const user = userEvent.setup();
    const { container } = render(<SuiteHistoryPanel {...props} />);
    expect(props.client.listSuites).not.toHaveBeenCalled();
    await open(user);
    expect(screen.getByText('fake/candidate · r2')).not.toBeNull();
    expect(screen.getByText('suite-decision-001')).not.toBeNull();
    expect(screen.getByText('Blocked')).not.toBeNull();
    expect(screen.getByText('1 run loaded · newest first')).not.toBeNull();
    expect(await axe.run(container)).toMatchObject({ violations: [] });
  });

  it('shows empty registration without making history requests', async () => {
    const props = harness();
    vi.mocked(props.client.listSuites).mockResolvedValue({
      data: { ...suitePage, items: [] },
      requestId: null,
    });
    render(<SuiteHistoryPanel {...props} />);
    await userEvent.click(
      screen.getByRole('button', { name: 'Browse suite history' }),
    );
    await screen.findByText(/No evaluation suites registered yet/);
    expect(props.client.listSuiteRuns).not.toHaveBeenCalled();
  });

  it('rejects a different digest and recovers only through an explicit retry', async () => {
    const props = harness();
    vi.mocked(props.client.listSuiteRuns).mockResolvedValueOnce({
      data: {
        ...suiteRunPage,
        items: [
          {
            ...suiteRunPage.items[0],
            suite: {
              ...suiteRunPage.items[0].suite,
              digest: `sha256:${'0'.repeat(64)}`,
            },
          },
        ],
      },
      requestId: null,
    });
    render(<SuiteHistoryPanel {...props} />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole('button', { name: 'Browse suite history' }),
    );
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Suite history could not be loaded',
    );
    expect(screen.queryByText('suite-run-002')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Refresh suites' }));
    await screen.findByText('fake/candidate · r2');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('revokes the session immediately even if a sibling read never completes', async () => {
    const props = harness();
    let siblingSignal: AbortSignal | undefined;
    vi.mocked(props.client.listSuiteRuns).mockRejectedValue(
      new ControlPlaneApiError({
        code: 'permission_denied',
        status: 403,
        message: 'This session cannot access the selected project.',
      }),
    );
    vi.mocked(props.client.listSuiteDecisions).mockImplementation(
      (_query, signal) => {
        siblingSignal = signal;
        return new Promise(() => undefined);
      },
    );
    render(<SuiteHistoryPanel {...props} />);
    await userEvent.click(
      screen.getByRole('button', { name: 'Browse suite history' }),
    );
    await waitFor(() =>
      expect(props.onAuthenticationFailure).toHaveBeenCalledTimes(1),
    );
    expect(siblingSignal?.aborted).toBe(true);
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('aborts reads on unmount and ignores late results', async () => {
    const props = harness();
    let resolve:
      | ((result: Awaited<ReturnType<typeof props.client.listSuites>>) => void)
      | undefined;
    let signal: AbortSignal | undefined;
    vi.mocked(props.client.listSuites).mockImplementation(
      (_query, currentSignal) => {
        signal = currentSignal;
        return new Promise((done) => {
          resolve = done;
        });
      },
    );
    const { unmount } = render(<SuiteHistoryPanel {...props} />);
    await userEvent.click(
      screen.getByRole('button', { name: 'Browse suite history' }),
    );
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve?.({ data: suitePage, requestId: null }));
    expect(props.client.listSuiteRuns).not.toHaveBeenCalled();
    expect(props.onAuthenticationFailure).not.toHaveBeenCalled();
  });

  it('keeps the selected revision when an older request finishes late', async () => {
    const props = harness();
    vi.mocked(props.client.listSuites).mockResolvedValue({
      data: {
        ...suitePage,
        items: [suitePage.items[0], { ...suitePage.items[0], revision: 2 }],
      },
      requestId: null,
    });
    render(<SuiteHistoryPanel {...props} />);
    const user = userEvent.setup();
    await open(user);
    let resolve:
      | ((
          result: Awaited<ReturnType<typeof props.client.listSuiteRuns>>,
        ) => void)
      | undefined;
    let staleSignal: AbortSignal | undefined;
    vi.mocked(props.client.listSuiteRuns).mockImplementationOnce(
      (_query, signal) => {
        staleSignal = signal;
        return new Promise((done) => {
          resolve = done;
        });
      },
    );
    vi.mocked(props.client.listSuiteDecisions).mockResolvedValueOnce({
      data: { ...suiteDecisionPage, items: [] },
      requestId: null,
    });
    await user.selectOptions(
      screen.getByLabelText('Suite revision'),
      'release/core@2',
    );
    expect(screen.queryByText('suite-run-002')).toBeNull();
    await user.selectOptions(
      screen.getByLabelText('Suite revision'),
      'release/core@1',
    );
    await screen.findByText('fake/candidate · r2');
    expect(staleSignal?.aborted).toBe(true);
    await act(async () =>
      resolve?.({
        data: {
          ...suiteRunPage,
          items: [
            {
              ...suiteRunPage.items[0],
              run_id: 'stale-run',
              suite: { ...suiteRunPage.items[0].suite, revision: 2 },
            },
          ],
        },
        requestId: null,
      }),
    );
    expect(screen.queryByText('stale-run')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(
      (screen.getByLabelText('Suite revision') as unknown as { value: string }).value,
    ).toBe('release/core@1');
  });

  it('pages the catalog and decisions independently without reloading runs', async () => {
    const props = harness();
    vi.mocked(props.client.listSuites)
      .mockResolvedValueOnce({
        data: { ...suitePage, next_cursor: 'catalog-next' },
        requestId: null,
      })
      .mockResolvedValueOnce({
        data: { ...suitePage, items: [{ ...suitePage.items[0], revision: 2 }] },
        requestId: null,
      });
    vi.mocked(props.client.listSuiteDecisions)
      .mockResolvedValueOnce({
        data: { ...suiteDecisionPage, next_cursor: 'decisions-next' },
        requestId: null,
      })
      .mockResolvedValueOnce({
        data: {
          ...suiteDecisionPage,
          items: [
            {
              ...suiteDecisionPage.items[0],
              decision_id: 'suite-decision-000',
            },
          ],
        },
        requestId: null,
      });
    render(<SuiteHistoryPanel {...props} />);
    const user = userEvent.setup();
    await open(user);
    await user.click(screen.getByRole('button', { name: 'Load more suites' }));
    await screen.findByRole('option', { name: 'release/core · revision 2' });
    await user.click(
      screen.getByRole('button', { name: 'Load older decisions' }),
    );
    await screen.findByText('suite-decision-000');
    expect(
      screen.getByText('2 decisions loaded · newest first'),
    ).not.toBeNull();
    expect(props.client.listSuiteRuns).toHaveBeenCalledTimes(1);
    expect(props.client.listSuites).toHaveBeenLastCalledWith(
      { cursor: 'catalog-next', limit: 20 },
      expect.any(AbortSignal),
    );
    expect(props.client.listSuiteDecisions).toHaveBeenLastCalledWith(
      {
        suite_name: 'release/core',
        suite_revision: 1,
        cursor: 'decisions-next',
        limit: 20,
      },
      expect.any(AbortSignal),
    );
  });

  it('rejects duplicate pagination without losing already verified evidence', async () => {
    const props = harness();
    vi.mocked(props.client.listSuiteRuns).mockResolvedValue({
      data: { ...suiteRunPage, next_cursor: 'page-2' },
      requestId: null,
    });
    render(<SuiteHistoryPanel {...props} />);
    const user = userEvent.setup();
    await open(user);
    await user.click(screen.getByRole('button', { name: 'Load older runs' }));
    await screen.findByRole('alert');
    expect(screen.getAllByText('fake/candidate · r2')).toHaveLength(1);
    expect(props.client.listSuiteRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ cursor: 'page-2', limit: 20 }),
      expect.any(AbortSignal),
    );
  });

  it('caps retained history at 100 records and never offers an unbounded fetch', async () => {
    const props = harness();
    let page = 0;
    vi.mocked(props.client.listSuiteRuns).mockImplementation(async () => {
      const offset = page++ * 20;
      return {
        data: {
          ...suiteRunPage,
          next_cursor: `page-${page}`,
          items: Array.from({ length: 20 }, (_, index) => ({
            ...suiteRunPage.items[0],
            run_id: `run-${String(100 - offset - index).padStart(3, '0')}`,
          })),
        },
        requestId: null,
      };
    });
    render(<SuiteHistoryPanel {...props} />);
    const user = userEvent.setup();
    await open(user);
    for (let index = 0; index < 4; index += 1) {
      await user.click(screen.getByRole('button', { name: 'Load older runs' }));
      await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
    }
    expect(screen.getByText('100 runs loaded · newest first')).not.toBeNull();
    expect(
      screen.queryByRole('button', { name: 'Load older runs' }),
    ).toBeNull();
    expect(props.client.listSuiteRuns).toHaveBeenCalledTimes(5);
  });
});
