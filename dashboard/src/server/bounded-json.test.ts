// @vitest-environment node

import { describe, expect, it } from 'vitest';

import { readBoundedJson } from './bounded-json';
import { HostedBoundaryError } from './hosted-boundary-error';

const encoder = new TextEncoder();

function chunkedResponse(
  chunks: readonly Uint8Array[],
  headers: Record<string, string> = { 'Content-Type': 'application/json' },
  counters?: { cancels: number; pulls: number },
): Response {
  let index = 0;
  return new Response(
    new ReadableStream<Uint8Array>({
      cancel() {
        if (counters) counters.cancels += 1;
      },
      pull(controller) {
        if (counters) counters.pulls += 1;
        if (index >= chunks.length) {
          controller.close();
          return;
        }
        controller.enqueue(chunks[index]);
        index += 1;
      },
    }),
    { headers },
  );
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
  throw new Error('Expected a bounded JSON error.');
}

describe('bounded upstream JSON', () => {
  it('decodes valid JSON split across stream chunks', async () => {
    const response = chunkedResponse([
      encoder.encode('{"items":'),
      encoder.encode('[1,2],"next_cursor":null}'),
    ]);

    await expect(readBoundedJson(response)).resolves.toEqual({
      items: [1, 2],
      next_cursor: null,
    });
  });

  it.each([
    'application/json',
    'application/json;charset=utf-8',
    'Application/JSON; Charset=UTF-8',
  ])('accepts the strict JSON media type %s', async (contentType) => {
    const response = chunkedResponse([encoder.encode('{"ok":true}')], {
      'Content-Type': contentType,
    });

    await expect(readBoundedJson(response)).resolves.toEqual({ ok: true });
  });

  it('rejects a declared oversized body before reading it', async () => {
    const counters = { cancels: 0, pulls: 0 };
    const response = chunkedResponse(
      [encoder.encode('{"private":"sentinel"}')],
      {
        'Content-Length': '1025',
        'Content-Type': 'application/json',
      },
      counters,
    );

    await boundaryError(
      () => readBoundedJson(response, 1024),
      'unexpected_upstream_response',
      502,
    );
    expect(counters.cancels).toBe(1);
    expect(counters.pulls).toBe(0);
  });

  it('enforces the actual byte ceiling when length is absent', async () => {
    const counters = { cancels: 0, pulls: 0 };
    const response = chunkedResponse(
      [encoder.encode('{"value":"'), encoder.encode('x'.repeat(40)), encoder.encode('"}')],
      { 'Content-Type': 'application/json' },
      counters,
    );

    await boundaryError(
      () => readBoundedJson(response, 32),
      'unexpected_upstream_response',
      502,
    );
    expect(counters.cancels).toBe(1);
  });

  it('enforces the actual byte ceiling when declared length lies', async () => {
    const counters = { cancels: 0, pulls: 0 };
    const response = chunkedResponse(
      [encoder.encode('{"value":"'), encoder.encode('x'.repeat(40)), encoder.encode('"}')],
      {
        'Content-Length': '2',
        'Content-Type': 'application/json',
      },
      counters,
    );

    await boundaryError(
      () => readBoundedJson(response, 32),
      'unexpected_upstream_response',
      502,
    );
    expect(counters.cancels).toBe(1);
  });

  it.each([
    ['missing media type', {}],
    ['HTML', { 'Content-Type': 'text/html' }],
    ['JSONP', { 'Content-Type': 'application/jsonp' }],
    ['extra parameter', { 'Content-Type': 'application/json; profile=private' }],
    ['empty declared body', { 'Content-Length': '0', 'Content-Type': 'application/json' }],
    ['padded length', { 'Content-Length': '01', 'Content-Type': 'application/json' }],
    ['joined lengths', { 'Content-Length': '1, 2', 'Content-Type': 'application/json' }],
  ] as const)('rejects %s without exposing response content', async (_label, headers) => {
    const privateSentinel = 'private-upstream-sentinel';
    const response = chunkedResponse(
      [encoder.encode(`{"value":"${privateSentinel}"}`)],
      headers,
    );

    const error = await boundaryError(
      () => readBoundedJson(response),
      'unexpected_upstream_response',
      502,
    );
    expect(`${error.message}${JSON.stringify(error)}${error.stack}`).not.toContain(
      privateSentinel,
    );
  });

  it('rejects an absent response body', async () => {
    const response = new Response(null, {
      headers: { 'Content-Type': 'application/json' },
    });

    await boundaryError(
      () => readBoundedJson(response),
      'unexpected_upstream_response',
      502,
    );
  });

  it.each([
    ['empty stream', new Uint8Array()],
    ['invalid UTF-8', new Uint8Array([0xc3, 0x28])],
    ['invalid JSON', encoder.encode('{private}')],
  ] as const)('rejects %s', async (_label, bytes) => {
    const response = chunkedResponse([bytes]);

    await boundaryError(
      () => readBoundedJson(response),
      'unexpected_upstream_response',
      502,
    );
  });

  it('sanitizes a stream failure and its private cause', async () => {
    const privateCause = 'private network failure detail';
    const response = new Response(
      new ReadableStream<Uint8Array>({
        pull(controller) {
          controller.error(new Error(privateCause));
        },
      }),
      { headers: { 'Content-Type': 'application/json' } },
    );

    const error = await boundaryError(
      () => readBoundedJson(response),
      'upstream_unavailable',
      503,
    );
    expect(`${error.message}${JSON.stringify(error)}${error.stack}`).not.toContain(
      privateCause,
    );
  });

  it.each([0, -1, 1.5, Number.NaN, 1024 * 1024 + 1])(
    'rejects the invalid byte ceiling %s as server configuration',
    async (maximumBytes) => {
      const response = chunkedResponse([encoder.encode('{"ok":true}')]);

      await boundaryError(
        () => readBoundedJson(response, maximumBytes),
        'service_configuration_invalid',
        503,
      );
    },
  );
});
