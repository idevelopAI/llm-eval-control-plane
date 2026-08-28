const LOOPBACK_HOSTS = new Set(['127.0.0.1', '[::1]', 'localhost']);

/** Browser bearer entry is a Phase 7 local-development capability only. */
export function isLoopbackDashboardLocation(
  location: Pick<Location, 'hostname' | 'protocol'>,
): boolean {
  return location.protocol === 'http:' && LOOPBACK_HOSTS.has(location.hostname);
}
