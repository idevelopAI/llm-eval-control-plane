import type { NextConfig } from 'next';

import {
  PRIVATE_RESPONSE_HEADERS,
  PRODUCTION_SECURITY_HEADERS,
} from './src/security/production-headers';

const staticExport = process.env.CONTROL_PLANE_STATIC_EXPORT === '1';

const nextConfig: NextConfig = {
  ...(staticExport ? { output: 'export', images: { unoptimized: true } } : {}),
  // Static exports receive these policies from the host's routing config.
  ...(staticExport ? {} : { async headers() {
    return [
      {
        headers: [...PRODUCTION_SECURITY_HEADERS],
        source: '/:path*',
      },
      {
        headers: [
          ...PRODUCTION_SECURITY_HEADERS,
          ...PRIVATE_RESPONSE_HEADERS,
        ],
        source: '/',
      },
      {
        headers: [
          ...PRODUCTION_SECURITY_HEADERS,
          ...PRIVATE_RESPONSE_HEADERS,
        ],
        source: '/api/:path*',
      },
    ];
  } }),
};

export default nextConfig;
