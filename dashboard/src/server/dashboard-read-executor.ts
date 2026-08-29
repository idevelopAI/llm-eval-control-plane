import 'server-only';

import type { components } from '../api/generated/schema';
import {
  isReleaseDecision,
  isReleaseDecisionCasePage,
  isReleaseDecisionDistributions,
  isReleaseDecisionPage,
} from '../api/validation';
import { readBoundedJson } from './bounded-json';
import {
  buildDashboardReadUrl,
  type DashboardReadOperation,
} from './dashboard-read-operation';
import { HostedBoundaryError } from './hosted-boundary-error';
import type { HostedControlPlaneConfiguration } from './hosted-config';

type ReleaseDecision = components['schemas']['ReleaseDecisionResponse'];
type ReleaseDecisionCasePage = components['schemas']['ReleaseDecisionCasePage'];
type ReleaseDecisionDistributions =
  components['schemas']['ReleaseDecisionDistributionsResponse'];
type ReleaseDecisionPage = components['schemas']['ReleaseDecisionPage'];

export type DashboardReadData =
  | ReleaseDecision
  | ReleaseDecisionCasePage
  | ReleaseDecisionDistributions
  | ReleaseDecisionPage;

export type DashboardReadResult = Readonly<{
  data: DashboardReadData;
  requestId: string | null;
}>;

export type DashboardReadDependencies = Readonly<{
  fetch: typeof fetch;
}>;

export const HOSTED_READ_TIMEOUT_MS = 10_000;

const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const CURSOR_PATTERN = /^[A-Za-z0-9_-]{1,2048}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const UTC_TIMESTAMP_PATTERN =
  /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?Z$/;

function unexpectedResponse(): never {
  throw new HostedBoundaryError('unexpected_upstream_response');
}

async function cancelResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // Never expose a response-body or cancellation detail.
  }
}

function safeRequestId(value: string | null): string | null {
  return value !== null && REQUEST_ID_PATTERN.test(value) ? value : null;
}

function hasSafeCursor(value: string | null | undefined): boolean {
  return value === null || value === undefined || CURSOR_PATTERN.test(value);
}

function sortableTimestamp(value: string): string | null {
  const match = UTC_TIMESTAMP_PATTERN.exec(value);
  if (match === null || !Number.isFinite(Date.parse(value))) return null;
  return `${match[1]}.${(match[2] ?? '').padEnd(6, '0')}Z`;
}

function isDecisionPageOrdered(
  items: ReleaseDecisionPage['items'],
  order: 'asc' | 'desc',
): boolean {
  const keys = items.map((item) => {
    const timestamp = sortableTimestamp(item.created_at);
    if (timestamp === null || !IDENTIFIER_PATTERN.test(item.decision_id)) {
      return null;
    }
    return `${timestamp}\0${item.decision_id}`;
  });
  if (keys.some((key) => key === null)) return false;

  for (let index = 1; index < keys.length; index += 1) {
    const previous = keys[index - 1] as string;
    const current = keys[index] as string;
    if (order === 'asc' ? previous > current : previous < current) return false;
  }
  return true;
}

function createDeadline(sourceSignal?: AbortSignal): Readonly<{
  dispose(): void;
  signal: AbortSignal;
}> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (sourceSignal?.aborted) abort();
  else sourceSignal?.addEventListener('abort', abort, { once: true });
  const timeout = setTimeout(abort, HOSTED_READ_TIMEOUT_MS);

  return Object.freeze({
    dispose() {
      clearTimeout(timeout);
      sourceSignal?.removeEventListener('abort', abort);
    },
    signal: controller.signal,
  });
}

function requireActive(signal: AbortSignal): void {
  if (signal.aborted) {
    throw new HostedBoundaryError('upstream_unavailable');
  }
}

function validateData(
  operation: DashboardReadOperation,
  payload: unknown,
): DashboardReadData {
  switch (operation.kind) {
    case 'decision-list':
      if (
        !isReleaseDecisionPage(payload) ||
        !hasSafeCursor(payload.next_cursor) ||
        payload.items.length > (operation.query.limit ?? 50) ||
        !isDecisionPageOrdered(
          payload.items,
          operation.query.order ?? 'asc',
        ) ||
        (operation.query.status !== undefined &&
          payload.items.some((item) => item.status !== operation.query.status))
      ) {
        unexpectedResponse();
      }
      return payload;
    case 'decision-detail':
      if (
        !isReleaseDecision(payload) ||
        payload.decision_id !== operation.decisionId
      ) {
        unexpectedResponse();
      }
      return payload;
    case 'decision-cases':
      if (
        !isReleaseDecisionCasePage(payload) ||
        payload.decision_id !== operation.decisionId ||
        !hasSafeCursor(payload.next_cursor) ||
        payload.items.length > (operation.query.limit ?? 50) ||
        payload.items.some(
          (item) =>
            item.metric !== operation.query.metric ||
            (item.gate_slice ?? undefined) !== operation.query.gateSlice ||
            (operation.query.caseSlice !== undefined &&
              !item.slices.includes(operation.query.caseSlice)) ||
            (operation.query.change !== undefined &&
              item.change !== operation.query.change),
        )
      ) {
        unexpectedResponse();
      }
      return payload;
    case 'decision-distributions':
      if (
        !isReleaseDecisionDistributions(payload) ||
        payload.decision_id !== operation.decisionId ||
        payload.score.metric !== operation.query.metric ||
        (payload.score.gate_slice ?? undefined) !== operation.query.gateSlice
      ) {
        unexpectedResponse();
      }
      return payload;
    default:
      unexpectedResponse();
  }
}

/** Execute one prevalidated operation without accepting any browser headers. */
export async function executeDashboardRead(
  operation: DashboardReadOperation,
  configuration: HostedControlPlaneConfiguration,
  dependencies: DashboardReadDependencies,
  signal?: AbortSignal,
): Promise<DashboardReadResult> {
  const url = buildDashboardReadUrl(configuration, operation);
  const deadline = createDeadline(signal);
  try {
    let response: Response;
    try {
      response = await dependencies.fetch(url, {
        cache: 'no-store',
        headers: configuration.createUpstreamHeaders(),
        method: 'GET',
        redirect: 'error',
        signal: deadline.signal,
      });
    } catch {
      throw new HostedBoundaryError('upstream_unavailable');
    }
    requireActive(deadline.signal);

    if (response.redirected || response.status !== 200) {
      await cancelResponseBody(response);
      if (response.status === 404 && operation.kind !== 'decision-list') {
        throw new HostedBoundaryError('resource_not_found');
      }
      throw new HostedBoundaryError('upstream_unavailable');
    }

    const payload = await readBoundedJson(response);
    requireActive(deadline.signal);
    return Object.freeze({
      data: validateData(operation, payload),
      requestId: safeRequestId(response.headers.get('x-request-id')),
    });
  } finally {
    deadline.dispose();
  }
}
