import axe from 'axe-core';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { createControlPlaneClient, ControlPlaneApiError } from '@/src/api/client';
import { createSuiteComparisonClient } from '@/src/api/suite-comparison-client';
import { comparisonJob, comparisonDecision, selection } from '@/src/test/suite-comparison';
import { suitePage, suiteRunPage, suiteDecisionPage, suiteTargetPage, suitePairPage } from '@/src/test/suite-history';
import { SuiteComparisonPanel } from './suite-comparison-panel';
import { SuiteHistoryPanel } from './suite-history-panel';

const TEST_TOKEN = `cpk_${'W'.repeat(43)}`;
function harness() {
  const client = createControlPlaneClient(() => null);
  const comparisons = createSuiteComparisonClient(() => null);
  vi.spyOn(comparisons, 'submit').mockResolvedValue(comparisonJob);
  vi.spyOn(comparisons, 'getJob').mockResolvedValue({ ...comparisonJob, status: 'succeeded', attempt_count: 1 });
  vi.spyOn(client, 'getReleaseDecision').mockResolvedValue({ data: comparisonDecision, requestId: null });
  return { suite: selection.suite, runs: [selection.candidate, selection.baseline], projectId: 'project-test', client, comparisons, onAuthenticationFailure: vi.fn(), onReviewDecision: vi.fn() };
}
async function choose(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Choose runs to compare' }));
  await user.selectOptions(screen.getByLabelText('Baseline run'), selection.baseline.run_id);
  await user.selectOptions(screen.getByLabelText('Candidate run'), selection.candidate.run_id);
}
async function submit(user: ReturnType<typeof userEvent.setup>, name = 'Submit comparison') {
  expect((screen.getByLabelText('Comparison write credential') as HTMLInputElement).disabled).toBe(false);
  await user.type(screen.getByLabelText('Comparison write credential'), TEST_TOKEN);
  expect((screen.getByLabelText('Comparison write credential') as HTMLInputElement).value).toBe(TEST_TOKEN);
  expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(false);
  await user.click(screen.getByRole('button', { name }));
}

describe('explicit local comparison workflow', () => {
  it('does nothing until explicitly submitted, clears credentials, and refreshes manually', async () => {
    const props = harness(); const user = userEvent.setup();
    let captured = '';
    vi.mocked(props.comparisons.submit).mockImplementation(async (_selection, _key, credential) => {
      captured = credential()!.accessToken; return comparisonJob;
    });
    render(<SuiteComparisonPanel {...props} />);
    expect(props.comparisons.submit).not.toHaveBeenCalled();
    await choose(user);
    expect(props.comparisons.submit).not.toHaveBeenCalled();
    await submit(user);
    expect(await screen.findByText('Comparison job: queued')).not.toBeNull();
    expect(captured).toBe(TEST_TOKEN);
    const call = vi.mocked(props.comparisons.submit).mock.calls[0];
    expect(call[0]).toEqual(selection);
    expect(call[1]).toMatch(/^comparison-/);
    expect(call[2]()).toBeNull();
    expect(document.body.textContent).not.toContain(TEST_TOKEN);
    expect(props.comparisons.getJob).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Refresh comparison status' }));
    await screen.findByText('Comparison job: succeeded');
    expect(props.onReviewDecision).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Review comparison gates' }));
    await waitFor(() => expect(props.onReviewDecision).toHaveBeenCalledOnce());
    expect(props.onReviewDecision.mock.calls[0][0]).toMatchObject({ decision_id: comparisonDecision.decision_id, status: 'failed', suite: selection.baseline.suite });
    expect(props.onReviewDecision.mock.calls[0][1]).toMatchObject({ baseline_target: selection.baseline.target, candidate_target: selection.candidate.target });
  });

  it('does not auto-select a baseline and blocks incompatible evidence', async () => {
    const props = harness(); const user = userEvent.setup();
    render(<SuiteComparisonPanel {...props} />);
    await user.click(screen.getByRole('button', { name: 'Choose runs to compare' }));
    expect(screen.getByLabelText('Baseline run')).toHaveProperty('value', '');
    expect((screen.getByRole('button', { name: 'Submit comparison' }) as HTMLButtonElement).disabled).toBe(true);
    await user.selectOptions(screen.getByLabelText('Baseline run'), selection.baseline.run_id);
    await user.selectOptions(screen.getByLabelText('Candidate run'), selection.baseline.run_id);
    expect(screen.getByRole('alert').textContent).toContain('two different runs');
    expect((screen.getByRole('button', { name: 'Submit comparison' }) as HTMLButtonElement).disabled).toBe(true);
    expect(props.comparisons.submit).not.toHaveBeenCalled();
  });

  it('preserves exact inputs and idempotency key after an uncertain response', async () => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.comparisons.submit).mockRejectedValueOnce(new Error('private-error-sentinel'));
    render(<SuiteComparisonPanel {...props} />); await choose(user); await submit(user);
    await screen.findByRole('alert');
    expect(screen.getByRole('alert').textContent).not.toContain('private-error-sentinel');
    expect((screen.getByLabelText('Comparison write credential') as HTMLInputElement).value).toBe('');
    expect(screen.getByLabelText('Baseline run')).toHaveProperty('disabled', true);
    await submit(user, 'Retry same comparison');
    expect(props.comparisons.submit).toHaveBeenCalledTimes(2);
    await expect(vi.mocked(props.comparisons.submit).mock.results[1].value).resolves.toEqual(comparisonJob);
    await screen.findByText('Comparison job: queued');
    const calls = vi.mocked(props.comparisons.submit).mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[0].slice(0, 2)).toEqual(calls[1].slice(0, 2));
    expect(calls[0][2]()).toBeNull(); expect(calls[1][2]()).toBeNull();
  });

  it.each([401, 403])('drops the entire session on denied write access (%i)', async (status) => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.comparisons.submit).mockRejectedValue(new ControlPlaneApiError({ status, code: 'permission_denied', message: 'private-sentinel' }));
    render(<SuiteComparisonPanel {...props} />); await choose(user); await submit(user);
    await waitFor(() => expect(props.onAuthenticationFailure).toHaveBeenCalledOnce());
    expect(screen.queryByLabelText('Comparison write credential')).toBeNull();
    expect(document.body.textContent).not.toContain('private-sentinel');
    expect(vi.mocked(props.comparisons.submit).mock.calls[0][2]()).toBeNull();
  });

  it('aborts on unmount and does not accept a late submission result', async () => {
    const props = harness(); const user = userEvent.setup();
    let resolve!: (value: typeof comparisonJob) => void;
    vi.mocked(props.comparisons.submit).mockImplementation(() => new Promise((done) => { resolve = done; }));
    const { unmount } = render(<SuiteComparisonPanel {...props} />); await choose(user); await submit(user);
    const call = vi.mocked(props.comparisons.submit).mock.calls[0];
    unmount(); expect(call[3].aborted).toBe(true); expect(call[2]()).toBeNull();
    await act(async () => resolve(comparisonJob));
    expect(props.onReviewDecision).not.toHaveBeenCalled();
  });

  it('blocks double submission while a request is in flight', async () => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.comparisons.submit).mockImplementation(() => new Promise(() => undefined));
    const { unmount } = render(<SuiteComparisonPanel {...props} />); await choose(user);
    await user.type(screen.getByLabelText('Comparison write credential'), TEST_TOKEN);
    await user.dblClick(screen.getByRole('button', { name: 'Submit comparison' }));
    expect(props.comparisons.submit).toHaveBeenCalledOnce();
    expect((screen.getByLabelText('Comparison write credential') as HTMLInputElement).value).toBe('');
    unmount();
  });

  it.each(['failed', 'canceled'] as const)('does not open gate review for a %s job', async (status) => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.comparisons.submit).mockResolvedValue({ ...comparisonJob, status });
    render(<SuiteComparisonPanel {...props} />); await choose(user); await submit(user);
    await screen.findByText(`Comparison job: ${status}`);
    expect(screen.queryByRole('button', { name: 'Review comparison gates' })).toBeNull();
  });

  it('rejects a decision that does not match the selected result digest', async () => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.comparisons.submit).mockResolvedValue({ ...comparisonJob, status: 'succeeded' });
    vi.mocked(props.client.getReleaseDecision).mockResolvedValue({ data: { ...comparisonDecision, baseline_result_digest: `sha256:${'0'.repeat(64)}` }, requestId: null });
    render(<SuiteComparisonPanel {...props} />); await choose(user); await submit(user);
    await user.click(await screen.findByRole('button', { name: 'Review comparison gates' }));
    await screen.findByRole('alert'); expect(props.onReviewDecision).not.toHaveBeenCalled();
  });

  it('clears the local session if access is lost during a job refresh', async () => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.comparisons.getJob).mockRejectedValue(new ControlPlaneApiError({ status: 403, code: 'permission_denied', message: 'private' }));
    render(<SuiteComparisonPanel {...props} />); await choose(user); await submit(user);
    await user.click(await screen.findByRole('button', { name: 'Refresh comparison status' }));
    await waitFor(() => expect(props.onAuthenticationFailure).toHaveBeenCalledOnce());
  });

  it('clears selection immediately when parent history is refreshed', async () => {
    const props = harness(); const user = userEvent.setup();
    vi.spyOn(props.client, 'listSuites').mockResolvedValue({ data: suitePage, requestId: null });
    vi.spyOn(props.client, 'listSuiteRuns').mockResolvedValue({ data: { ...suiteRunPage, items: props.runs }, requestId: null });
    vi.spyOn(props.client, 'listSuiteDecisions').mockResolvedValue({ data: suiteDecisionPage, requestId: null });
    vi.spyOn(props.client, 'listSuiteTargets').mockResolvedValue({ data: suiteTargetPage, requestId: null });
    vi.spyOn(props.client, 'listSuiteTargetPairs').mockResolvedValue({ data: suitePairPage, requestId: null });
    render(<SuiteHistoryPanel {...props} />);
    await user.click(screen.getByRole('button', { name: 'Browse suite history' }));
    await screen.findByRole('button', { name: 'Choose runs to compare' });
    await choose(user);
    await user.click(screen.getByRole('button', { name: 'Refresh suites' }));
    await screen.findByRole('button', { name: 'Choose runs to compare' });
    expect(screen.queryByLabelText('Baseline run')).toBeNull();
  });

  it('has accessible controls without provider or credential persistence', async () => {
    const user = userEvent.setup(); const props = harness();
    const { container } = render(<SuiteComparisonPanel {...props} />);
    await choose(user);
    expect(await axe.run(container)).toMatchObject({ violations: [] });
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
  });
});
