import { HostedBoundaryError } from './hosted-boundary-error';

export const PLATFORM_USER_ID_HEADER = 'oai-authenticated-user-id';

export type HeaderSource = Pick<Headers, 'get'>;

// The dispatcher contract defines this value as opaque. Keep only a transport
// bound: visible ASCII, no comma-joined duplicates, and no normalization.
const USER_ID_PATTERN = /^[\x21-\x2B\x2D-\x7E]{1,256}$/;

function isCanonicalUserId(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length <= 256 &&
    value.trim() === value &&
    USER_ID_PATTERN.test(value)
  );
}

/**
 * Authorize exactly one Site-scoped platform user without retaining email,
 * display name, cookie, or app-owned session state.
 */
export function requirePlatformOwner(
  headers: HeaderSource,
  expectedUserId: string,
): void {
  if (!isCanonicalUserId(expectedUserId)) {
    throw new HostedBoundaryError('service_configuration_invalid');
  }

  let presentedUserId: string | null;
  try {
    presentedUserId = headers.get(PLATFORM_USER_ID_HEADER);
  } catch {
    throw new HostedBoundaryError('authentication_required');
  }

  if (!isCanonicalUserId(presentedUserId)) {
    throw new HostedBoundaryError('authentication_required');
  }
  if (presentedUserId !== expectedUserId) {
    throw new HostedBoundaryError('permission_denied');
  }
}
