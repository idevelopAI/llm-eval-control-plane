import { describe, expect, it } from 'vitest';

import { HostedBoundaryError } from './hosted-boundary-error';
import {
  PLATFORM_USER_ID_HEADER,
  requirePlatformOwner,
} from './platform-identity';

const OWNER_ID = 'account-user_01:site-owner';

function boundaryError(
  action: () => void,
  code: HostedBoundaryError['code'],
  status: number,
) {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(HostedBoundaryError);
    expect(error).toMatchObject({ code, status });
    return error as HostedBoundaryError;
  }
  throw new Error('Expected a hosted boundary error.');
}

describe('platform Site identity', () => {
  it('authorizes only the exact configured Site-scoped user', () => {
    const headers = new Headers({ [PLATFORM_USER_ID_HEADER]: OWNER_ID });

    expect(requirePlatformOwner(headers, OWNER_ID)).toBeUndefined();
  });

  it.each([
    ['missing', null],
    ['empty', ''],
    ['leading whitespace', ` ${OWNER_ID}`],
    ['trailing whitespace', `${OWNER_ID} `],
    ['comma-joined', `${OWNER_ID},another-user`],
    ['non-ASCII', 'üser'],
    ['embedded space', 'opaque user'],
    ['control character', 'opaque\u0001user'],
    ['oversized', 'x'.repeat(257)],
  ] as const)('rejects %s as an unauthenticated identity', (_label, value) => {
    const headers = { get: () => value };

    boundaryError(
      () => requirePlatformOwner(headers, OWNER_ID),
      'authentication_required',
      401,
    );
  });

  it('does not accept email as a substitute for the stable user ID', () => {
    const headers = new Headers({
      'oai-authenticated-user-email': 'owner@example.test',
    });

    boundaryError(
      () => requirePlatformOwner(headers, OWNER_ID),
      'authentication_required',
      401,
    );
  });

  it('denies a different valid platform user', () => {
    const headers = new Headers({
      [PLATFORM_USER_ID_HEADER]: 'account-user_02:other',
    });

    boundaryError(
      () => requirePlatformOwner(headers, OWNER_ID),
      'permission_denied',
      403,
    );
  });

  it('fails closed with a content-free error when owner configuration is invalid', () => {
    const headers = new Headers({ [PLATFORM_USER_ID_HEADER]: OWNER_ID });
    const invalidOwner = ` ${OWNER_ID}`;

    const error = boundaryError(
      () => requirePlatformOwner(headers, invalidOwner),
      'service_configuration_invalid',
      503,
    );

    expect(JSON.stringify(error)).not.toContain(OWNER_ID);
    expect(JSON.stringify(error)).not.toContain(invalidOwner);
  });

  it('converts header-reader failures into a content-free authentication error', () => {
    const headers = {
      get() {
        throw new Error('private dispatcher detail');
      },
    };

    const error = boundaryError(
      () => requirePlatformOwner(headers, OWNER_ID),
      'authentication_required',
      401,
    );

    expect(error.message).not.toContain('private dispatcher detail');
  });
});
