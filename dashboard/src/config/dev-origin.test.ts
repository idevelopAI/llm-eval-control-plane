import { describe, expect, it } from 'vitest';

import { resolveControlPlaneDevOrigin } from './dev-origin';

describe('resolveControlPlaneDevOrigin', () => {
  it('defaults to the local control-plane port', () => {
    expect(resolveControlPlaneDevOrigin()).toBe('http://127.0.0.1:8000');
  });

  it.each([
    ['http://127.0.0.1:9100', 'http://127.0.0.1:9100'],
    ['http://localhost:9100', 'http://localhost:9100'],
    ['http://[::1]:9100', 'http://[::1]:9100'],
  ])('accepts the loopback origin %s', (input, expected) => {
    expect(resolveControlPlaneDevOrigin(input)).toBe(expected);
  });

  it.each([
    '',
    'https://127.0.0.1:9100',
    'http://0.0.0.0:9100',
    'http://localhost.example:9100',
    'http://example.test:9100',
    'http://localhost',
    'http://user:password@localhost:9100',
    'http://localhost:9100/api',
    'http://localhost:9100/?token=private',
    'http://localhost:9100/#private',
  ])('rejects unsafe proxy target %#', (input) => {
    expect(() => resolveControlPlaneDevOrigin(input)).toThrow(
      'CONTROL_PLANE_DEV_ORIGIN must be an explicit loopback HTTP origin.',
    );
  });
});
