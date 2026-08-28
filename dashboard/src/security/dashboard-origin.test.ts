import { describe, expect, it } from 'vitest';

import { isLoopbackDashboardLocation } from './dashboard-origin';

describe('isLoopbackDashboardLocation', () => {
  it.each(['localhost', '127.0.0.1', '[::1]'])(
    'allows HTTP loopback host %s',
    (hostname) => {
      expect(
        isLoopbackDashboardLocation({ hostname, protocol: 'http:' }),
      ).toBe(true);
    },
  );

  it.each([
    { hostname: 'localhost', protocol: 'https:' },
    { hostname: 'localhost.example', protocol: 'http:' },
    { hostname: '0.0.0.0', protocol: 'http:' },
    { hostname: 'dashboard.example', protocol: 'https:' },
  ])('rejects hosted or non-loopback location %#', (location) => {
    expect(isLoopbackDashboardLocation(location)).toBe(false);
  });
});
