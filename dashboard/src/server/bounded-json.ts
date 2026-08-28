import { HostedBoundaryError } from './hosted-boundary-error';

export const MAXIMUM_HOSTED_JSON_BYTES = 1024 * 1024;

const CONTENT_LENGTH_PATTERN = /^(?:0|[1-9][0-9]*)$/;
const JSON_MEDIA_TYPE_PATTERN =
  /^application\/json(?:\s*;\s*charset\s*=\s*utf-8)?$/i;

function unexpectedResponse(): HostedBoundaryError {
  return new HostedBoundaryError('unexpected_upstream_response');
}

async function cancelBody(body: ReadableStream<Uint8Array> | null): Promise<void> {
  try {
    await body?.cancel();
  } catch {
    // Cancellation failures are intentionally content-free.
  }
}

function declaredLength(response: Response): number | null {
  const value = response.headers.get('content-length');
  if (value === null) return null;
  if (!CONTENT_LENGTH_PATTERN.test(value)) throw unexpectedResponse();

  const length = Number(value);
  if (!Number.isSafeInteger(length)) throw unexpectedResponse();
  return length;
}

/** Read one successful JSON projection with an actual decoded byte ceiling. */
export async function readBoundedJson(
  response: Response,
  maximumBytes = MAXIMUM_HOSTED_JSON_BYTES,
): Promise<unknown> {
  if (
    !Number.isInteger(maximumBytes) ||
    maximumBytes < 1 ||
    maximumBytes > MAXIMUM_HOSTED_JSON_BYTES
  ) {
    throw new HostedBoundaryError('service_configuration_invalid');
  }

  const contentType = response.headers.get('content-type');
  if (contentType === null || !JSON_MEDIA_TYPE_PATTERN.test(contentType)) {
    await cancelBody(response.body);
    throw unexpectedResponse();
  }

  let expectedLength: number | null;
  try {
    expectedLength = declaredLength(response);
  } catch (error) {
    await cancelBody(response.body);
    throw error;
  }
  if (expectedLength !== null && (expectedLength === 0 || expectedLength > maximumBytes)) {
    await cancelBody(response.body);
    throw unexpectedResponse();
  }
  if (response.body === null) throw unexpectedResponse();

  let reader: ReadableStreamDefaultReader<Uint8Array>;
  try {
    reader = response.body.getReader();
  } catch {
    throw new HostedBoundaryError('upstream_unavailable');
  }

  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > maximumBytes) {
        try {
          await reader.cancel();
        } catch {
          // Preserve the bounded response error rather than a stream detail.
        }
        throw unexpectedResponse();
      }
      chunks.push(value);
    }
  } catch (error) {
    if (error instanceof HostedBoundaryError) throw error;
    try {
      await reader.cancel();
    } catch {
      // Preserve the content-free availability error below.
    }
    throw new HostedBoundaryError('upstream_unavailable');
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // A released or failed stream does not change the public result.
    }
  }

  if (totalBytes === 0) throw unexpectedResponse();
  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }

  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw unexpectedResponse();
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw unexpectedResponse();
  }
}
