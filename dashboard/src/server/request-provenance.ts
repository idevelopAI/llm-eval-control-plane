import { HostedBoundaryError } from './hosted-boundary-error';
import type { HostedControlPlaneConfiguration } from './hosted-config';

export type HostedReadRequest = Readonly<{
  headers: Pick<Headers, 'get'>;
  method: string;
  url: string;
}>;

function requestNotAllowed(): never {
  throw new HostedBoundaryError('request_not_allowed');
}

function headerValue(headers: Pick<Headers, 'get'>, name: string): string | null {
  try {
    return headers.get(name);
  } catch {
    requestNotAllowed();
  }
}

/**
 * Require a platform-authenticated, same-origin browser GET before a future
 * route can construct any upstream request.
 */
export function requireHostedReadProvenance(
  request: HostedReadRequest,
  configuration: HostedControlPlaneConfiguration,
): void {
  if (request.method !== 'GET') {
    throw new HostedBoundaryError('method_not_allowed');
  }

  let requestUrl: URL;
  try {
    requestUrl = new URL(request.url);
  } catch {
    requestNotAllowed();
  }

  const expectedOrigin = configuration.siteOrigin();
  if (
    requestUrl.origin !== expectedOrigin ||
    requestUrl.username !== '' ||
    requestUrl.password !== '' ||
    requestUrl.hash !== ''
  ) {
    requestNotAllowed();
  }

  if (
    headerValue(request.headers, 'sec-fetch-site') !== 'same-origin' ||
    headerValue(request.headers, 'sec-fetch-mode') !== 'same-origin' ||
    headerValue(request.headers, 'sec-fetch-dest') !== 'empty'
  ) {
    requestNotAllowed();
  }

  const origin = headerValue(request.headers, 'origin');
  if (origin !== null && origin !== expectedOrigin) {
    requestNotAllowed();
  }

  if (
    headerValue(request.headers, 'authorization') !== null ||
    headerValue(request.headers, 'x-project-id') !== null
  ) {
    requestNotAllowed();
  }

  configuration.authorizeOwner(request.headers);
}
