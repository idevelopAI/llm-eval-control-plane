import { HostedBoundaryError } from './hosted-boundary-error';
import type { HostedControlPlaneConfiguration } from './hosted-config';

type ListOrder = 'asc' | 'desc';
type ReleaseStatus = 'failed' | 'passed';
type CaseChange =
  | 'incomparable'
  | 'newly_failing'
  | 'newly_passing'
  | 'unchanged_failing'
  | 'unchanged_passing';

type DecisionListQuery = Readonly<{
  cursor?: string;
  limit?: number;
  order?: ListOrder;
  status?: ReleaseStatus;
}>;

type CaseQuery = Readonly<{
  caseSlice?: string;
  change?: CaseChange;
  cursor?: string;
  gateSlice?: string;
  limit?: number;
  metric: string;
}>;

type DistributionQuery = Readonly<{
  gateSlice?: string;
  metric: string;
}>;

export type DecisionDetailOperation = Readonly<{
  decisionId: string;
  kind: 'decision-detail';
}>;
export type DecisionDistributionOperation = Readonly<{
  decisionId: string;
  kind: 'decision-distributions';
  query: DistributionQuery;
}>;
export type DecisionListOperation = Readonly<{
  kind: 'decision-list';
  query: DecisionListQuery;
}>;
export type DecisionCaseOperation = Readonly<{
  decisionId: string;
  kind: 'decision-cases';
  query: CaseQuery;
}>;

export type DashboardReadOperation =
  | DecisionCaseOperation
  | DecisionDetailOperation
  | DecisionDistributionOperation
  | DecisionListOperation;

const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const METRIC_PATTERN = /^[A-Za-z][A-Za-z0-9._:/-]{0,127}$/;
const SLICE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/=-]{0,127}$/;
const CURSOR_PATTERN = /^[A-Za-z0-9_-]{1,2048}$/;
const LIMIT_PATTERN = /^(?:[1-9]|[1-9][0-9]|100)$/;
const CASE_CHANGES = new Set<CaseChange>([
  'incomparable',
  'newly_failing',
  'newly_passing',
  'unchanged_failing',
  'unchanged_passing',
]);
const LIST_ORDERS = new Set<ListOrder>(['asc', 'desc']);
const RELEASE_STATUSES = new Set<ReleaseStatus>(['failed', 'passed']);

function invalidRequest(): never {
  throw new HostedBoundaryError('invalid_request');
}

function canonicalValue(
  params: URLSearchParams,
  key: string,
  pattern: RegExp,
  required: true,
): string;
function canonicalValue(
  params: URLSearchParams,
  key: string,
  pattern: RegExp,
  required?: false,
): string | undefined;
function canonicalValue(
  params: URLSearchParams,
  key: string,
  pattern: RegExp,
  required = false,
): string | undefined {
  const values = params.getAll(key);
  if (values.length === 0) {
    if (required) invalidRequest();
    return undefined;
  }
  if (values.length !== 1 || !pattern.test(values[0])) invalidRequest();
  return values[0];
}

function canonicalEnum<T extends string>(
  params: URLSearchParams,
  key: string,
  values: ReadonlySet<T>,
): T | undefined {
  const value = canonicalValue(params, key, /^[A-Za-z_]+$/);
  if (value === undefined) return undefined;
  if (!values.has(value as T)) invalidRequest();
  return value as T;
}

function canonicalLimit(params: URLSearchParams): number | undefined {
  const value = canonicalValue(params, 'limit', LIMIT_PATTERN);
  return value === undefined ? undefined : Number(value);
}

function requireAllowedKeys(
  params: URLSearchParams,
  allowed: ReadonlySet<string>,
): void {
  for (const key of params.keys()) {
    if (!allowed.has(key) || params.getAll(key).length !== 1) invalidRequest();
  }
}

function canonicalDecisionId(value: string): string {
  if (!IDENTIFIER_PATTERN.test(value)) invalidRequest();
  return value;
}

function frozen<T extends object>(value: T): Readonly<T> {
  return Object.freeze(value);
}

export function parseDecisionListQuery(
  params: URLSearchParams,
): DecisionListOperation {
  requireAllowedKeys(params, new Set(['cursor', 'limit', 'order', 'status']));
  return frozen({
    kind: 'decision-list' as const,
    query: frozen({
      cursor: canonicalValue(params, 'cursor', CURSOR_PATTERN),
      limit: canonicalLimit(params),
      order: canonicalEnum(params, 'order', LIST_ORDERS),
      status: canonicalEnum(params, 'status', RELEASE_STATUSES),
    }),
  });
}

export function parseDecisionDetail(
  decisionId: string,
  params: URLSearchParams = new URLSearchParams(),
): DecisionDetailOperation {
  requireAllowedKeys(params, new Set());
  return frozen({
    decisionId: canonicalDecisionId(decisionId),
    kind: 'decision-detail' as const,
  });
}

export function parseDecisionCases(
  decisionId: string,
  params: URLSearchParams,
): DecisionCaseOperation {
  requireAllowedKeys(
    params,
    new Set(['case_slice', 'change', 'cursor', 'gate_slice', 'limit', 'metric']),
  );
  return frozen({
    decisionId: canonicalDecisionId(decisionId),
    kind: 'decision-cases' as const,
    query: frozen({
      caseSlice: canonicalValue(params, 'case_slice', SLICE_PATTERN),
      change: canonicalEnum(params, 'change', CASE_CHANGES),
      cursor: canonicalValue(params, 'cursor', CURSOR_PATTERN),
      gateSlice: canonicalValue(params, 'gate_slice', SLICE_PATTERN),
      limit: canonicalLimit(params),
      metric: canonicalValue(params, 'metric', METRIC_PATTERN, true),
    }),
  });
}

export function parseDecisionDistributions(
  decisionId: string,
  params: URLSearchParams,
): DecisionDistributionOperation {
  requireAllowedKeys(params, new Set(['gate_slice', 'metric']));
  return frozen({
    decisionId: canonicalDecisionId(decisionId),
    kind: 'decision-distributions' as const,
    query: frozen({
      gateSlice: canonicalValue(params, 'gate_slice', SLICE_PATTERN),
      metric: canonicalValue(params, 'metric', METRIC_PATTERN, true),
    }),
  });
}

function setOptional(
  searchParams: URLSearchParams,
  key: string,
  value: string | number | undefined,
): void {
  if (value !== undefined) searchParams.set(key, String(value));
}

/** Build one of four fixed upstream reads; no caller supplies an upstream path. */
export function buildDashboardReadUrl(
  configuration: HostedControlPlaneConfiguration,
  operation: DashboardReadOperation,
): URL {
  const url = new URL(configuration.upstreamOrigin());

  switch (operation.kind) {
    case 'decision-list':
      url.pathname = '/v1/release-decisions';
      setOptional(url.searchParams, 'limit', operation.query.limit);
      setOptional(url.searchParams, 'cursor', operation.query.cursor);
      setOptional(url.searchParams, 'status', operation.query.status);
      setOptional(url.searchParams, 'order', operation.query.order);
      return url;
    case 'decision-detail':
      url.pathname = `/v1/release-decisions/${encodeURIComponent(operation.decisionId)}`;
      return url;
    case 'decision-cases':
      url.pathname = `/v1/release-decisions/${encodeURIComponent(operation.decisionId)}/cases`;
      setOptional(url.searchParams, 'metric', operation.query.metric);
      setOptional(url.searchParams, 'limit', operation.query.limit);
      setOptional(url.searchParams, 'cursor', operation.query.cursor);
      setOptional(url.searchParams, 'gate_slice', operation.query.gateSlice);
      setOptional(url.searchParams, 'case_slice', operation.query.caseSlice);
      setOptional(url.searchParams, 'change', operation.query.change);
      return url;
    case 'decision-distributions':
      url.pathname = `/v1/release-decisions/${encodeURIComponent(operation.decisionId)}/distributions`;
      setOptional(url.searchParams, 'metric', operation.query.metric);
      setOptional(url.searchParams, 'gate_slice', operation.query.gateSlice);
      return url;
    default:
      invalidRequest();
  }
}
