type ResponseHeader = Readonly<{ key: string; value: string }>;

const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'none'",
  "connect-src 'self'",
  "font-src 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "img-src 'self' data:",
  "manifest-src 'self'",
  "object-src 'none'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "worker-src 'self'",
  'upgrade-insecure-requests',
].join('; ');

export const PRODUCTION_SECURITY_HEADERS: readonly ResponseHeader[] =
  Object.freeze([
    { key: 'Content-Security-Policy', value: contentSecurityPolicy },
    {
      key: 'Cross-Origin-Opener-Policy',
      value: 'same-origin',
    },
    {
      key: 'Cross-Origin-Resource-Policy',
      value: 'same-origin',
    },
    {
      key: 'Permissions-Policy',
      value:
        'browsing-topics=(), camera=(), geolocation=(), microphone=(), payment=(), usb=()',
    },
    { key: 'Referrer-Policy', value: 'no-referrer' },
    {
      key: 'Strict-Transport-Security',
      value: 'max-age=63072000; includeSubDomains; preload',
    },
    { key: 'X-Content-Type-Options', value: 'nosniff' },
    { key: 'X-Frame-Options', value: 'DENY' },
  ]);

export const PRIVATE_RESPONSE_HEADERS: readonly ResponseHeader[] = Object.freeze([
  { key: 'Cache-Control', value: 'private, no-store, max-age=0' },
]);
