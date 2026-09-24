import type { components } from './generated/schema';
import { ControlPlaneApiError } from './client';
import { isLoopbackDashboardLocation } from '../security/dashboard-origin';
import { isRuntimeCredential, type CredentialSource, type RuntimeCredential } from '../security/runtime-credential-vault';

export type LocalJob = components['schemas']['JobResponse'];
type Kind = 'run' | 'comparison';
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const STATES = new Set(['queued', 'running', 'cancel_requested', 'succeeded', 'failed', 'canceled']);
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
export const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
export const keys = (value: Record<string, unknown>, allowed: string[]) =>
  Object.keys(value).every((key) => allowed.includes(key));
const id = (value: unknown): value is string => typeof value === 'string' && ID.test(value);
const count = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const time = (value: unknown): value is string =>
  typeof value === 'string' && value.length <= 40 &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);

export function isLocalJob(value: unknown, kind: Kind): value is LocalJob {
  return record(value) && keys(value, [
    'schema_version', 'job_id', 'kind', 'status', 'resource_id', 'attempt_count',
    'max_attempts', 'available_at', 'error_code', 'created_at', 'updated_at',
  ]) && value.schema_version === 'job/v2' && value.kind === kind &&
    id(value.job_id) && id(value.resource_id) && typeof value.status === 'string' && STATES.has(value.status) &&
    count(value.attempt_count) && count(value.max_attempts) && value.max_attempts > 0 &&
    value.attempt_count <= value.max_attempts && time(value.available_at) && time(value.created_at) &&
    time(value.updated_at) && Date.parse(value.updated_at) >= Date.parse(value.created_at) &&
    (value.error_code == null || (typeof value.error_code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value.error_code)));
}

export function localJobError(status: number, code = 'unexpected_response') {
  return new ControlPlaneApiError({ status, code, message:
    status === 401 || status === 403 ? 'Local job access was denied. Reconnect the local session.' :
    status === 409 ? 'The job conflicts with an earlier submission. Do not retry with changed inputs.' :
    'The local job request could not be verified. Refresh its status or retry the same submission.' });
}

async function boundedJson(response: Response): Promise<unknown> {
  if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json' ||
      Number(response.headers.get('content-length')) > MAX_RESPONSE_BYTES || !response.body) {
    await response.body?.cancel().catch(() => undefined);
    throw localJobError(response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let size = 0;
  let body = '';
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > MAX_RESPONSE_BYTES) throw localJobError(response.status);
      body += decoder.decode(chunk.value, { stream: true });
    }
    return JSON.parse(body + decoder.decode()) as unknown;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

/** Fixed local job endpoints only, not an arbitrary fetch proxy. */
export function createLocalJobClient(readCredential: CredentialSource, kind: Kind) {
  async function request(route: string, authSource: CredentialSource, signal: AbortSignal, body?: object, key?: string) {
    if (typeof location === 'undefined' || !isLoopbackDashboardLocation(location)) throw localJobError(403, 'local_only');
    let read: RuntimeCredential | null;
    let auth: RuntimeCredential | null;
    try {
      read = readCredential();
      auth = authSource();
    } catch {
      throw localJobError(401, 'authentication_required');
    }
    if (!isRuntimeCredential(read) || !isRuntimeCredential(auth)) throw localJobError(401, 'authentication_required');
    if (read.projectId !== auth.projectId) throw localJobError(403, 'permission_denied');
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal.addEventListener('abort', cancel, { once: true });
    if (signal.aborted) controller.abort();
    const timeout = setTimeout(cancel, 30_000);
    try {
      const response = await fetch(new URL(route, location.origin), {
        method: body ? 'POST' : 'GET', signal: controller.signal, cache: 'no-store', credentials: 'omit',
        mode: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer',
        headers: {
          Authorization: `Bearer ${auth.accessToken}`, 'X-Project-ID': auth.projectId,
          ...(body ? { 'Content-Type': 'application/json', 'Idempotency-Key': key! } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (!(body ? [200, 202] : [200]).includes(response.status)) {
        void response.body?.cancel().catch(() => undefined);
        throw localJobError(response.status);
      }
      return await boundedJson(response);
    } catch (error) {
      if (signal.aborted) throw new DOMException('Request canceled.', 'AbortError');
      if (error instanceof ControlPlaneApiError) throw error;
      throw localJobError(0, 'network_error');
    } finally {
      controller.abort();
      clearTimeout(timeout);
      signal.removeEventListener('abort', cancel);
    }
  }
  return {
    submit(body: object, key: string, writeCredential: CredentialSource, signal: AbortSignal) {
      return request(kind === 'run' ? '/v1/suite-runs' : '/v1/suite-comparisons', writeCredential, signal, body, key);
    },
    async getJob(previous: LocalJob, signal: AbortSignal): Promise<LocalJob> {
      if (!isLocalJob(previous, kind)) throw localJobError(400);
      const value = await request(`/v1/jobs/${encodeURIComponent(previous.job_id)}`, readCredential, signal);
      if (!isLocalJob(value, kind) || value.job_id !== previous.job_id || value.resource_id !== previous.resource_id ||
          value.created_at !== previous.created_at || value.max_attempts !== previous.max_attempts ||
          value.attempt_count < previous.attempt_count || Date.parse(value.updated_at) < Date.parse(previous.updated_at) ||
          (['succeeded', 'failed', 'canceled'].includes(previous.status) && value.status !== previous.status)) throw localJobError(200);
      return value;
    },
  };
}
