import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSuiteRunClient, runSelectionIssue } from './suite-run-client';
import { runJob, runSelection, runSummary } from '../test/suite-run';

const read = () => ({ projectId: 'project-test', accessToken: `cpk_${'R'.repeat(43)}` });
const write = () => ({ projectId: 'project-test', accessToken: `cpk_${'W'.repeat(43)}` });
const key = ['run', '12345678', '1234', '4234', '8234', '123456789012'].join('-');
const signal = () => new AbortController().signal;
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
const submission = () => ({ schema_version: 'run-submission/v2', job: runJob, run: null });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('local offline run client', () => {
  it.each([200, 202])('posts only the selected suite and fixed target identity (%i)', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => json(submission(), status)));
    expect(await createSuiteRunClient(read).submit(runSelection, key, write, signal())).toEqual(runJob);
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toBe(`${location.origin}/v1/suite-runs`);
    expect(init).toMatchObject({ method: 'POST', cache: 'no-store', credentials: 'omit', mode: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer', headers: {
      Authorization: `Bearer ${write().accessToken}`, 'X-Project-ID': 'project-test', 'Idempotency-Key': key,
    } });
    expect(JSON.parse(init!.body as string)).toEqual({ suite_name: 'release/core', suite_revision: 1, target_name: 'fake/candidate', target_revision: 2 });
  });

  it('blocks invalid suite metadata, live suites, unknown targets, and malformed keys before sending', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    const client = createSuiteRunClient(read);
    for (const selection of [
      { ...runSelection, targetId: '' }, { ...runSelection, targetId: 'provider/live' },
      { ...runSelection, suite: { ...runSelection.suite, execution_mode: 'live' as const } },
      { ...runSelection, suite: { ...runSelection.suite, revision: 0 } },
    ]) {
      expect(runSelectionIssue(selection)).not.toBeNull();
      await expect(client.submit(selection, key, write, signal())).rejects.toMatchObject({ status: 400 });
    }
    await expect(client.submit(runSelection, '../wrong', write, signal())).rejects.toMatchObject({ status: 400 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(['https://example.com', 'http://localhost.example.com', 'https://localhost'])('denies a write on %s', async (url) => {
    vi.stubGlobal('location', new URL(url));
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    await expect(createSuiteRunClient(read).submit(runSelection, key, write, signal())).rejects.toMatchObject({ status: 403 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('requires a valid read session and a same-project write credential', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    await expect(createSuiteRunClient(() => null).submit(runSelection, key, write, signal())).rejects.toMatchObject({ status: 401 });
    await expect(createSuiteRunClient(read).submit(runSelection, key, () => ({ ...write(), projectId: 'other' }), signal())).rejects.toMatchObject({ status: 403 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('validates terminal replay identity and returns only the job', async () => {
    const job = { ...runJob, status: 'succeeded' as const };
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...submission(), job, run: runSummary })));
    expect(await createSuiteRunClient(read).submit(runSelection, key, write, signal())).toEqual(job);
    for (const change of [
      { run_id: 'other' }, { suite: null }, { execution_mode: 'live' },
      { target: { ...runSummary.target, revision: 9 } }, { result_digest: 'not-a-digest' },
      { dataset: { ...runSummary.dataset, revision: 9 } }, { private_payload: 'private-sentinel' },
    ]) {
      vi.mocked(fetch).mockResolvedValueOnce(json({ ...submission(), job, run: { ...runSummary, ...change } }));
      await expect(createSuiteRunClient(read).submit(runSelection, key, write, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
    }
  });

  it('rejects a different job kind and unexpected submission fields', async () => {
    for (const value of [{ ...submission(), job: { ...runJob, kind: 'comparison' } }, { ...submission(), payload: 'private' }]) {
      vi.stubGlobal('fetch', vi.fn(async () => json(value)));
      await expect(createSuiteRunClient(read).submit(runSelection, key, write, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
    }
  });

  it('refreshes with the read credential and rejects a changed resource or terminal regression', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...runJob, status: 'running', attempt_count: 1 })));
    const client = createSuiteRunClient(read);
    expect((await client.getJob(runJob, signal())).status).toBe('running');
    expect(vi.mocked(fetch).mock.calls[0][1]).toMatchObject({ method: 'GET', headers: { Authorization: `Bearer ${read().accessToken}` } });
    vi.mocked(fetch).mockResolvedValueOnce(json({ ...runJob, resource_id: 'other' }));
    await expect(client.getJob(runJob, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
    await expect(client.getJob({ ...runJob, status: 'succeeded' }, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
  });

  it.each([401, 403, 409, 422, 500])('never displays the private error body (%i)', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ message: 'private-sentinel' }, status)));
    const error = await createSuiteRunClient(read).submit(runSelection, key, write, signal()).catch((value: unknown) => value);
    expect(error).toMatchObject({ status });
    expect(String(error)).not.toContain('private-sentinel');
  });
});
