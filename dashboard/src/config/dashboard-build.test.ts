// @vitest-environment node
import { PHASE_DEVELOPMENT_SERVER, PHASE_PRODUCTION_BUILD, PHASE_PRODUCTION_SERVER } from 'next/constants.js';
import { afterEach, describe, expect, it, vi } from 'vitest';
import nextConfig from '../../next.config';

afterEach(() => vi.unstubAllEnvs());

describe('native dashboard build boundary', () => {
  it.each([PHASE_PRODUCTION_BUILD, PHASE_PRODUCTION_SERVER, 'unknown-phase'])('keeps %s static with no proxy or live substitution', (phase) => {
    vi.stubEnv('CONTROL_PLANE_DEV_ORIGIN', 'https://untrusted.example');
    expect(nextConfig(phase)).toEqual({ output: 'export', images: { unoptimized: true } });
  });
  it('restricts development rewrites to the validated loopback API', async () => {
    vi.stubEnv('CONTROL_PLANE_DEV_ORIGIN', 'http://127.0.0.1:9100');
    const config = nextConfig(PHASE_DEVELOPMENT_SERVER);
    expect(config.output).toBeUndefined();
    expect(config.agentRules).toBe(false);
    expect(config.webpack).toBeTypeOf('function');
    expect(await config.rewrites?.()).toEqual([
      { source: '/health/:path*', destination: 'http://127.0.0.1:9100/health/:path*' },
      { source: '/openapi.json', destination: 'http://127.0.0.1:9100/openapi.json' },
      { source: '/v1/:path*', destination: 'http://127.0.0.1:9100/v1/:path*' },
    ]);
  });
  it('rejects a remote development proxy before the server starts', () => {
    vi.stubEnv('CONTROL_PLANE_DEV_ORIGIN', 'https://untrusted.example');
    expect(() => nextConfig(PHASE_DEVELOPMENT_SERVER)).toThrow('explicit loopback HTTP origin');
  });
});
