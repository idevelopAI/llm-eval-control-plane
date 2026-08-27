const DEFAULT_CONTROL_PLANE_DEV_ORIGIN = 'http://127.0.0.1:8000';
const LOOPBACK_HOSTS = new Set(['127.0.0.1', '[::1]', 'localhost']);

/**
 * Keep the development proxy on the same machine. This prevents an accidental
 * environment override from forwarding browser authorization headers to a
 * remote host.
 */
export function resolveControlPlaneDevOrigin(value?: string): string {
  const candidate = value ?? DEFAULT_CONTROL_PLANE_DEV_ORIGIN;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new Error(
      'CONTROL_PLANE_DEV_ORIGIN must be an explicit loopback HTTP origin.',
    );
  }

  const isOriginOnly =
    url.pathname === '/' && !url.search && !url.hash && !url.username && !url.password;
  if (
    url.protocol !== 'http:' ||
    !LOOPBACK_HOSTS.has(url.hostname) ||
    !url.port ||
    !isOriginOnly
  ) {
    throw new Error(
      'CONTROL_PLANE_DEV_ORIGIN must be an explicit loopback HTTP origin.',
    );
  }
  return url.origin;
}
