import { afterEach, describe, expect, it, vi } from 'vitest';
import { comparisonIssue, createSuiteComparisonClient, isComparisonJob, matchesComparisonDecision } from './suite-comparison-client';
import { comparisonJob, comparisonDecision, selection } from '../test/suite-comparison';

const read = () => ({ projectId: 'project-test', accessToken: `cpk_${'A'.repeat(43)}` });
const write = () => ({ projectId: 'project-test', accessToken: `cpk_${'B'.repeat(43)}` });
// Synthetic retry identifier, not a bearer credential.
const key = ['comparison', '12345678', '1234', '4234', '8234', '123456789012'].join('-');
const signal = () => new AbortController().signal;
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
const submission = () => ({ schema_version: 'comparison-submission/v2', job: comparisonJob, decision: null });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('isolated local comparison client', () => {
  it.each([200, 202])('submits a bounded exact body with a separate credential (%i)', async (status) => {
    const fetch = vi.fn(async () => json(submission(), status));
    vi.stubGlobal('fetch', fetch);
    const client = createSuiteComparisonClient(read);
    expect(await client.submit(selection, key, write, signal())).toEqual(comparisonJob);
    const [url, init] = vi.mocked(globalThis.fetch).mock.calls[0];
    expect(String(url)).toBe(`${location.origin}/v1/suite-comparisons`);
    expect(init).toMatchObject({ method: 'POST', cache: 'no-store', credentials: 'omit', mode: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer', headers: {
      Authorization: `Bearer ${write().accessToken}`, 'X-Project-ID': 'project-test', 'Idempotency-Key': key,
    } });
    expect(JSON.parse(init!.body as string)).toEqual({ suite_name: selection.suite.name, suite_revision: 1, baseline_run_id: 'suite-run-001', candidate_run_id: 'suite-run-002' });
    expect(init!.body).not.toContain(write().accessToken);
  });

  it.each(['https://example.com', 'http://example.com', 'https://localhost', 'http://localhost.evil.test'])('denies non-loopback capability at %s', async (url) => {
    vi.stubGlobal('location', new URL(url));
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    await expect(createSuiteComparisonClient(read).submit(selection, key, write, signal())).rejects.toMatchObject({ status: 403 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('requires both credentials and the exact read-session project', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    for (const source of [() => null, () => ({ ...write(), projectId: 'another-project' }), () => ({ ...write(), accessToken: 'invalid' })]) {
      await expect(createSuiteComparisonClient(read).submit(selection, key, source, signal())).rejects.toBeInstanceOf(Error);
    }
    await expect(createSuiteComparisonClient(() => null).submit(selection, key, write, signal())).rejects.toMatchObject({ status: 401 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects incompatible runs and malformed keys before sending', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    const variants = [
      { ...selection, candidate: selection.baseline },
      { ...selection, candidate: { ...selection.candidate, suite: { ...selection.candidate.suite, digest: `sha256:${'0'.repeat(64)}` } } },
      { ...selection, candidate: { ...selection.candidate, dataset_revision: 7 } },
      { ...selection, candidate: { ...selection.candidate, execution_mode: 'live' as const } },
      { ...selection, candidate: { ...selection.candidate, target: { ...selection.candidate.target, digest: null } } },
    ];
    for (const item of variants) {
      expect(comparisonIssue(item)).not.toBeNull();
      await expect(createSuiteComparisonClient(read).submit(item, key, write, signal())).rejects.toMatchObject({ status: 400 });
    }
    await expect(createSuiteComparisonClient(read).submit(selection, '../wrong', write, signal())).rejects.toMatchObject({ status: 400 });
    expect(fetch).not.toHaveBeenCalled();
    expect(comparisonIssue({ ...selection, candidate: { ...selection.candidate, status: 'completed_with_failures' } })).toBeNull();
  });

  it('sanitizes credential-source failures before making any request', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    const unavailable = () => { throw new Error('private-credential-sentinel'); };
    for (const [readSource, writeSource] of [[unavailable, write], [read, unavailable]]) {
      const error = await createSuiteComparisonClient(readSource)
        .submit(selection, key, writeSource, signal()).catch((value: unknown) => value);
      expect(error).toMatchObject({ status: 401 });
      expect(String(error)).not.toContain('private-credential-sentinel');
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    { kind: 'run' }, { status: 'unknown' }, { resource_id: '../private' },
    { attempt_count: 4 }, { max_attempts: 0 }, { attempt_count: 0.5 },
    { available_at: 'yesterday' }, { error_code: 'private exception with spaces' }, { payload: 'private-sentinel' },
  ])('rejects invalid/private job fields %j', async (change) => {
    const value = { ...comparisonJob, ...change };
    expect(isComparisonJob(value)).toBe(false);
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...submission(), job: value })));
    await expect(createSuiteComparisonClient(read).submit(selection, key, write, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
  });

  it('checks exact terminal decision provenance, including both result digests', async () => {
    const job = { ...comparisonJob, status: 'succeeded' as const };
    expect(matchesComparisonDecision(comparisonDecision, selection, job)).toBe(true);
    for (const change of [
      { baseline_result_digest: `sha256:${'0'.repeat(64)}` }, { candidate_run_id: 'other' },
      { decision_id: 'wrong' }, { suite: null }, { candidate: selection.baseline.target },
    ]) expect(matchesComparisonDecision({ ...comparisonDecision, ...change }, selection, job)).toBe(false);
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...submission(), job, decision: comparisonDecision })));
    expect(await createSuiteComparisonClient(read).submit(selection, key, write, signal())).toEqual(job);
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...submission(), job, decision: { ...comparisonDecision, candidate_run_id: 'wrong' } })));
    await expect(createSuiteComparisonClient(read).submit(selection, key, write, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
  });

  it('uses only the read credential for job refresh and rejects changed job identity', async () => {
    const fetch = vi.fn(async () => json({ ...comparisonJob, status: 'running', attempt_count: 1 })); vi.stubGlobal('fetch', fetch);
    const client = createSuiteComparisonClient(read);
    expect((await client.getJob(comparisonJob, signal())).status).toBe('running');
    expect(vi.mocked(globalThis.fetch).mock.calls[0][1]).toMatchObject({ method: 'GET', headers: { Authorization: `Bearer ${read().accessToken}` } });
    for (const change of [{ job_id: 'another' }, { resource_id: 'another' }, { created_at: '2026-09-15T12:21:00Z' }, { max_attempts: 4 }]) {
      fetch.mockResolvedValueOnce(json({ ...comparisonJob, ...change }));
      await expect(client.getJob(comparisonJob, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
    }
  });

  it.each([401, 403, 409, 422, 500])('sanitizes private server errors (%i)', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ error: { message: 'private-secret-sentinel' } }, status)));
    const error = await createSuiteComparisonClient(read).submit(selection, key, write, signal()).catch((value: unknown) => value);
    expect(error).toMatchObject({ status }); expect(String(error)).not.toContain('private-secret-sentinel');
  });

  it('rejects time, attempt, and terminal-status regression on job refresh', async () => {
    const previous = { ...comparisonJob, status: 'succeeded' as const, attempt_count: 2 };
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    const client = createSuiteComparisonClient(read);
    for (const change of [
      { attempt_count: 1 }, { updated_at: '2026-09-15T12:19:00Z' },
      { status: 'queued' }, { status: 'failed' }, { status: 'canceled' },
    ]) {
      fetch.mockResolvedValueOnce(json({ ...previous, ...change }));
      await expect(client.getJob(previous, signal())).rejects.toMatchObject({ code: 'unexpected_response' });
    }
  });

  it('rejects unexpected fields, content type, and oversized responses', async () => {
    for (const response of [json({ ...submission(), payload: 'private' }), new Response('not json'),
      new Response(JSON.stringify(submission()), { headers: { 'content-type': 'application/json-invalid' } }),
      new Response('x'.repeat(2 * 1024 * 1024 + 1), { headers: { 'content-type': 'application/json' } })]) {
      vi.stubGlobal('fetch', vi.fn(async () => response));
      await expect(createSuiteComparisonClient(read).submit(selection, key, write, signal())).rejects.toBeInstanceOf(Error);
    }
  });

  it('bounds a stalled submission and treats an explicit abort separately', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal!.addEventListener('abort', () => reject(new Error('private-network-sentinel')));
    })));
    const operation = createSuiteComparisonClient(read).submit(selection, key, write, signal());
    const result = expect(operation).rejects.toMatchObject({ code: 'network_error' });
    await vi.advanceTimersByTimeAsync(30_000); await result;
    const controller = new AbortController();
    const canceled = createSuiteComparisonClient(read).submit(selection, key, write, controller.signal);
    const cancellation = expect(canceled).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort(); await cancellation;
  });
});
