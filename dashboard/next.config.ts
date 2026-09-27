import type { NextConfig } from 'next';
import { PHASE_DEVELOPMENT_SERVER } from 'next/constants.js';
import { fileURLToPath } from 'node:url';

import { resolveControlPlaneDevOrigin } from './src/config/dev-origin';

export default function nextConfig(phase: string): NextConfig {
  // Every non-development build is a public static export. No environment flag
  // can opt a production build into local credentials or API proxying.
  if (phase !== PHASE_DEVELOPMENT_SERVER) {
    return { output: 'export', images: { unoptimized: true } };
  }

  const origin = resolveControlPlaneDevOrigin(process.env.CONTROL_PLANE_DEV_ORIGIN);
  const localEntry = fileURLToPath(new URL('./app/release-dashboard.tsx', import.meta.url));

  return {
    // Development and smoke tests must not generate instruction files in source.
    agentRules: false,
    // Used only by `next dev --webpack`, which binds to loopback in package.json.
    webpack(config) {
      config.resolve.alias['./public-release-dashboard$'] = localEntry;
      return config;
    },
    async rewrites() {
      return [
        { source: '/health/:path*', destination: `${origin}/health/:path*` },
        { source: '/openapi.json', destination: `${origin}/openapi.json` },
        { source: '/v1/:path*', destination: `${origin}/v1/:path*` },
      ];
    },
  };
}
