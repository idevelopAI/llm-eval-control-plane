import type { NextConfig } from 'next';

import {
  PRIVATE_RESPONSE_HEADERS,
  PRODUCTION_SECURITY_HEADERS,
} from './src/security/production-headers';

const nextConfig: NextConfig = {
  async headers() {
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
  },
};

export default nextConfig;
