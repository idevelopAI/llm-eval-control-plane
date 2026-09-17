import { afterEach, describe, expect, it, vi } from 'vitest';

import { createControlPlaneClient } from './client';
import {
  suiteDecisionPage,
  suitePage,
  suitePin,
  suiteRunPage,
} from '../test/suite-history';

const TEST_TOKEN = `cpk_${'A'.repeat(43)}`;
const credential = () => ({
  accessToken: TEST_TOKEN,
  projectId: 'project-test',
});
const query = {
  suite_name: suitePin.name,
  suite_revision: suitePin.revision,
  limit: 20,
};
const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { 'content-type': 'application/json' },
  });

afterEach(() => vi.unstubAllGlobals());

describe('read-only suite history client', () => {
  it('uses only same-origin authenticated GETs with encoded suite names', async () => {
    const fetch = vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname;
      return response(
        path === '/v1/suites'
          ? suitePage
          : path === '/v1/suite-runs'
            ? suiteRunPage
            : suiteDecisionPage,
      );
    });
    vi.stubGlobal('fetch', fetch);
    const client = createControlPlaneClient(credential);
    const results = await Promise.all([
      client.listSuites({ name: suitePin.name, limit: 20 }),
      client.listSuiteRuns(query),
      client.listSuiteDecisions(query),
    ]);
    expect(fetch).toHaveBeenCalledTimes(3);
    for (const [request] of fetch.mock.calls) {
      expect(request.method).toBe('GET');
      expect(request.url).not.toContain(TEST_TOKEN);
      expect(request.url).toContain('release%2Fcore');
      expect(new URL(request.url).origin).toBe(location.origin);
      expect(request.headers.get('authorization')).toBe(`Bearer ${TEST_TOKEN}`);
      expect(request.headers.get('x-project-id')).toBe('project-test');
      expect(request.cache).toBe('no-store');
      expect(request.redirect).toBe('error');
      expect(request.referrerPolicy).toBe('no-referrer');
      expect(request.mode).toBe('same-origin');
    }
    expect(JSON.stringify(results)).not.toContain(TEST_TOKEN);
  });

  it('does not send requests after the credential is cleared', async () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    const client = createControlPlaneClient(() => null);
    for (const request of [
      () => client.listSuites(),
      () => client.listSuiteRuns(query),
      () => client.listSuiteDecisions(query),
    ])
      await expect(request()).rejects.toMatchObject({
        status: 401,
        code: 'authentication_required',
      });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects private fields without echoing their values', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        response({ ...suiteRunPage, document: 'private-sentinel' }),
      ),
    );
    const error = await createControlPlaneClient(credential)
      .listSuiteRuns(query)
      .catch((error: unknown) => error);
    expect(error).toMatchObject({ code: 'unexpected_response' });
    expect(String(error)).not.toContain('private-sentinel');
  });

  it('rejects a valid page for the wrong requested suite', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(suiteRunPage))
      .mockResolvedValueOnce(response(suiteDecisionPage));
    vi.stubGlobal('fetch', fetch);
    const client = createControlPlaneClient(credential);
    await expect(
      client.listSuiteRuns({ ...query, suite_name: 'other' }),
    ).rejects.toMatchObject({ code: 'unexpected_response' });
    await expect(
      client.listSuiteDecisions({ ...query, suite_revision: 2 }),
    ).rejects.toMatchObject({ code: 'unexpected_response' });
  });

  it('rejects collection results beyond the requested limit or name filter', async () => {
    const other = { ...suitePage.items[0], name: 'other' };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        response({ ...suitePage, items: [suitePage.items[0], other] }),
      ),
    );
    const client = createControlPlaneClient(credential);
    await expect(client.listSuites({ limit: 1 })).rejects.toMatchObject({
      code: 'unexpected_response',
    });
    await expect(
      client.listSuites({ name: suitePin.name }),
    ).rejects.toMatchObject({ code: 'unexpected_response' });
  });

  it('forwards cancellation and never surfaces the transport exception', async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        controller.abort();
        throw new Error('private-transport-sentinel');
      }),
    );
    await expect(
      createControlPlaneClient(credential).listSuiteRuns(
        query,
        controller.signal,
      ),
    ).rejects.toMatchObject({
      name: 'AbortError',
      message: 'The request was canceled.',
    });
  });
});
