import 'server-only';

import { PRIVATE_RESPONSE_HEADERS } from '../security/production-headers';
import type { DashboardReadResult } from './dashboard-read-executor';
import { HostedBoundaryError } from './hosted-boundary-error';

const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const FALLBACK_ERROR_REQUEST_ID = 'hreq_unavailable';

function safeRequestId(value: string | null | undefined): string | null {
  return value !== null &&
    value !== undefined &&
    REQUEST_ID_PATTERN.test(value)
    ? value
    : null;
}

function privateJsonHeaders(requestId: string | null): Headers {
  const headers = new Headers({
    'Content-Type': 'application/json; charset=utf-8',
    'Cross-Origin-Resource-Policy': 'same-origin',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
  });
  for (const header of PRIVATE_RESPONSE_HEADERS) {
    headers.set(header.key, header.value);
  }
  if (requestId !== null) headers.set('X-Request-ID', requestId);
  return headers;
}

/** Build a fresh success response without retaining upstream response metadata. */
export function hostedReadSuccessResponse(result: DashboardReadResult): Response {
  const requestId = safeRequestId(result.requestId);
  return new Response(JSON.stringify(result.data), {
    headers: privateJsonHeaders(requestId),
    status: 200,
  });
}

/** Collapse any thrown value into a fixed, private JSON error response. */
export function hostedReadErrorResponse(
  thrown: unknown,
  requestId: string | null = null,
): Response {
  const error =
    thrown instanceof HostedBoundaryError
      ? thrown
      : new HostedBoundaryError('internal_error');
  const responseRequestId =
    safeRequestId(requestId) ?? FALLBACK_ERROR_REQUEST_ID;
  const headers = privateJsonHeaders(responseRequestId);
  for (const [name, value] of Object.entries(error.responseHeaders)) {
    headers.set(name, value);
  }

  return new Response(
    JSON.stringify({
      error: {
        code: error.code,
        details: [],
        message: error.message,
        request_id: responseRequestId,
      },
      schema_version: 'api-error/v1',
    }),
    { headers, status: error.status },
  );
}
