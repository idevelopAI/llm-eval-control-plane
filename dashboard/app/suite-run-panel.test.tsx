import axe from 'axe-core';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ControlPlaneApiError } from '@/src/api/client';
import { createSuiteRunClient } from '@/src/api/suite-run-client';
import { runJob, runSelection } from '@/src/test/suite-run';
import { SuiteRunPanel } from './suite-run-panel';

const token = `cpk_${'W'.repeat(43)}`;
function harness() {
  const client = createSuiteRunClient(() => null);
  vi.spyOn(client, 'submit').mockResolvedValue(runJob);
  vi.spyOn(client, 'getJob').mockResolvedValue({ ...runJob, status: 'succeeded', attempt_count: 1 });
  return { suite: runSelection.suite, projectId: 'project-test', client, onAuthenticationFailure: vi.fn(), onShowRuns: vi.fn() };
}
async function choose(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Start an offline run' }));
  await user.selectOptions(screen.getByLabelText('Offline target'), 'candidate');
}
async function submit(user: ReturnType<typeof userEvent.setup>, name = 'Submit offline run') {
  await user.type(screen.getByLabelText('Run write credential'), token);
  await user.click(screen.getByRole('button', { name }));
}

describe('explicit offline suite run form', () => {
  it('does not submit automatically, refreshes manually, and offers completed history', async () => {
    const props = harness(); const user = userEvent.setup();
    render(<SuiteRunPanel {...props} />);
    await user.click(screen.getByRole('button', { name: 'Start an offline run' }));
    expect(screen.getByLabelText('Offline target')).toHaveProperty('value', '');
    expect(screen.getByRole('button', { name: 'Submit offline run' })).toHaveProperty('disabled', true);
    await user.selectOptions(screen.getByLabelText('Offline target'), 'candidate');
    expect(props.client.submit).not.toHaveBeenCalled();
    await submit(user);
    await screen.findByText('Run job: queued');
    const call = vi.mocked(props.client.submit).mock.calls[0];
    expect(call[0]).toEqual(runSelection);
    expect(call[2]()).toBeNull();
    expect(props.client.getJob).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(token);
    await user.click(screen.getByRole('button', { name: 'Refresh run status' }));
    await screen.findByText('Run job: succeeded');
    expect(props.onShowRuns).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Show completed runs' }));
    expect(props.onShowRuns).toHaveBeenCalledOnce();
  });

  it('freezes inputs and retries the same key after an uncertain response', async () => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.client.submit).mockRejectedValueOnce(new Error('private-sentinel'));
    render(<SuiteRunPanel {...props} />); await choose(user); await submit(user);
    await screen.findByRole('alert');
    expect(screen.getByRole('alert').textContent).not.toContain('private-sentinel');
    expect(screen.getByLabelText('Offline target')).toHaveProperty('disabled', true);
    expect(screen.getByLabelText('Run write credential')).toHaveProperty('value', '');
    await submit(user, 'Retry same run');
    await screen.findByText('Run job: queued');
    const calls = vi.mocked(props.client.submit).mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[0].slice(0, 2)).toEqual(calls[1].slice(0, 2));
    expect(calls[1][2]()).toBeNull();
  });

  it.each([401, 403])('clears the whole session on lost write access (%i)', async (status) => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.client.submit).mockRejectedValue(new ControlPlaneApiError({ status, code: 'permission_denied', message: 'private' }));
    render(<SuiteRunPanel {...props} />); await choose(user); await submit(user);
    await waitFor(() => expect(props.onAuthenticationFailure).toHaveBeenCalledOnce());
    expect(screen.queryByLabelText('Run write credential')).toBeNull();
    expect(vi.mocked(props.client.submit).mock.calls[0][2]()).toBeNull();
  });

  it('clears on denied job reads, too', async () => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.client.getJob).mockRejectedValue(new ControlPlaneApiError({ status: 403, code: 'permission_denied', message: 'private' }));
    render(<SuiteRunPanel {...props} />); await choose(user); await submit(user);
    await user.click(await screen.findByRole('button', { name: 'Refresh run status' }));
    await waitFor(() => expect(props.onAuthenticationFailure).toHaveBeenCalledOnce());
  });

  it('blocks duplicate submissions and aborts late work on unmount', async () => {
    const props = harness(); const user = userEvent.setup();
    let resolve!: (value: typeof runJob) => void;
    vi.mocked(props.client.submit).mockImplementation(() => new Promise((done) => { resolve = done; }));
    const { unmount } = render(<SuiteRunPanel {...props} />); await choose(user);
    await user.type(screen.getByLabelText('Run write credential'), token);
    await user.dblClick(screen.getByRole('button', { name: 'Submit offline run' }));
    expect(props.client.submit).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('Run write credential')).toHaveProperty('value', '');
    const call = vi.mocked(props.client.submit).mock.calls[0];
    unmount(); expect(call[3].aborted).toBe(true); expect(call[2]()).toBeNull();
    await act(async () => resolve(runJob));
    expect(props.onShowRuns).not.toHaveBeenCalled();
  });

  it.each(['failed', 'canceled'] as const)('does not present a %s job as completed evidence', async (status) => {
    const props = harness(); const user = userEvent.setup();
    vi.mocked(props.client.submit).mockResolvedValue({ ...runJob, status });
    render(<SuiteRunPanel {...props} />); await choose(user); await submit(user);
    await screen.findByText(`Run job: ${status}`);
    expect(screen.queryByRole('button', { name: 'Show completed runs' })).toBeNull();
  });

  it('closes without canceling accepted jobs or storing credentials', async () => {
    const props = harness(); const user = userEvent.setup();
    const { container } = render(<SuiteRunPanel {...props} />); await choose(user);
    expect(await axe.run(container)).toMatchObject({ violations: [] });
    await submit(user); await screen.findByText('Run job: queued');
    await user.click(screen.getByRole('button', { name: 'Close run form' }));
    expect(screen.queryByText('Run job: queued')).toBeNull();
    expect(props.client.getJob).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
  });
});
