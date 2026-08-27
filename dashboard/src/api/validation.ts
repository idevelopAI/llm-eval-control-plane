import type { components } from './generated/schema';

type ReleaseDecision = components['schemas']['ReleaseDecisionResponse'];
type ReleaseDecisionCasePage = components['schemas']['ReleaseDecisionCasePage'];
type ReleaseDecisionDistributions =
  components['schemas']['ReleaseDecisionDistributionsResponse'];
type ReleaseDecisionPage = components['schemas']['ReleaseDecisionPage'];
type QuantileSummary = components['schemas']['QuantileSummaryResponse'];

const ARTIFACT_KINDS = new Set([
  'dataset',
  'target',
  'prompt',
  'evaluator',
  'suite',
  'gate_policy',
]);
const CASE_CHANGES = new Set([
  'newly_passing',
  'newly_failing',
  'unchanged_passing',
  'unchanged_failing',
  'incomparable',
]);
const COMPARISON_STATUSES = new Set(['scored', 'skipped', 'error']);
const EXECUTION_MODES = new Set([
  'offline_deterministic_fixture',
  'offline_mock',
  'live',
]);
const FAILURE_CODES = new Set(['coverage', 'threshold', 'regression']);
const METRIC_DIRECTIONS = new Set(['higher_is_better', 'lower_is_better']);
const RELEASE_STATUSES = new Set(['passed', 'failed']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

function isString(value: unknown): value is string {
  return typeof value === 'string';
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isCount(value: unknown): value is number {
  return isFiniteNumber(value) && Number.isInteger(value) && value >= 0;
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || value === null || isString(value);
}

function isOptionalNumber(value: unknown): boolean {
  return value === undefined || value === null || isFiniteNumber(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString);
}

function isArtifact(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, ['digest', 'kind', 'name', 'revision']) &&
    isOptionalString(value.digest) &&
    isString(value.kind) &&
    ARTIFACT_KINDS.has(value.kind) &&
    isString(value.name) &&
    isCount(value.revision) &&
    value.revision > 0
  );
}

function isMetricAggregate(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !hasOnlyKeys(value, [
      'attempted',
      'errors',
      'mean',
      'scored',
      'skipped',
    ]) ||
    !isCount(value.attempted) ||
    !isCount(value.errors) ||
    !isOptionalNumber(value.mean) ||
    !isCount(value.scored) ||
    !isCount(value.skipped)
  ) {
    return false;
  }
  return value.attempted === value.errors + value.scored + value.skipped;
}

function isAggregate(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'baseline',
      'candidate',
      'delta',
      'evaluator',
      'metric',
      'slice',
    ]) &&
    isMetricAggregate(value.baseline) &&
    isMetricAggregate(value.candidate) &&
    isOptionalNumber(value.delta) &&
    isArtifact(value.evaluator) &&
    isString(value.metric) &&
    isOptionalString(value.slice)
  );
}

function isGate(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'aggregate',
      'allowed_regression',
      'coverage_passed',
      'direction',
      'failure_codes',
      'metric',
      'regression_passed',
      'slice',
      'status',
      'threshold',
      'threshold_passed',
    ]) &&
    isAggregate(value.aggregate) &&
    isFiniteNumber(value.allowed_regression) &&
    typeof value.coverage_passed === 'boolean' &&
    isString(value.direction) &&
    METRIC_DIRECTIONS.has(value.direction) &&
    Array.isArray(value.failure_codes) &&
    value.failure_codes.every(
      (code) => isString(code) && FAILURE_CODES.has(code),
    ) &&
    isString(value.metric) &&
    typeof value.regression_passed === 'boolean' &&
    isOptionalString(value.slice) &&
    isString(value.status) &&
    RELEASE_STATUSES.has(value.status) &&
    isFiniteNumber(value.threshold) &&
    typeof value.threshold_passed === 'boolean'
  );
}

function isReleaseDecisionListItem(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'baseline_run_id',
      'candidate_run_id',
      'created_at',
      'decision_digest',
      'decision_id',
      'schema_version',
      'status',
    ]) &&
    value.schema_version === 'release-decision-list-item/v1' &&
    isString(value.baseline_run_id) &&
    isString(value.candidate_run_id) &&
    isString(value.created_at) &&
    isString(value.decision_digest) &&
    isString(value.decision_id) &&
    isString(value.status) &&
    RELEASE_STATUSES.has(value.status)
  );
}

export function isReleaseDecisionPage(
  value: unknown,
): value is ReleaseDecisionPage {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, ['items', 'next_cursor', 'schema_version']) &&
    value.schema_version === 'release-decision-page/v1' &&
    Array.isArray(value.items) &&
    value.items.every(isReleaseDecisionListItem) &&
    isOptionalString(value.next_cursor)
  );
}

export function isReleaseDecision(value: unknown): value is ReleaseDecision {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'aggregates',
      'baseline',
      'baseline_result_digest',
      'baseline_run_id',
      'candidate',
      'candidate_result_digest',
      'candidate_run_id',
      'created_at',
      'dataset',
      'decision_digest',
      'decision_id',
      'execution_mode',
      'gates',
      'schema_version',
      'spec_name',
      'status',
    ]) &&
    value.schema_version === 'release-decision-summary/v1' &&
    Array.isArray(value.aggregates) &&
    value.aggregates.every(isAggregate) &&
    isArtifact(value.baseline) &&
    isString(value.baseline_result_digest) &&
    isString(value.baseline_run_id) &&
    isArtifact(value.candidate) &&
    isString(value.candidate_result_digest) &&
    isString(value.candidate_run_id) &&
    isString(value.created_at) &&
    isArtifact(value.dataset) &&
    isString(value.decision_digest) &&
    isString(value.decision_id) &&
    isString(value.execution_mode) &&
    EXECUTION_MODES.has(value.execution_mode) &&
    Array.isArray(value.gates) &&
    value.gates.every(isGate) &&
    isString(value.spec_name) &&
    isString(value.status) &&
    RELEASE_STATUSES.has(value.status)
  );
}

function isComparisonValue(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !hasOnlyKeys(value, ['status', 'value']) ||
    !isString(value.status) ||
    !COMPARISON_STATUSES.has(value.status)
  ) {
    return false;
  }
  return value.status === 'scored'
    ? isFiniteNumber(value.value)
    : value.value === undefined || value.value === null;
}

function isReleaseDecisionCase(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'baseline',
      'baseline_passed',
      'candidate',
      'candidate_passed',
      'case_id',
      'change',
      'delta',
      'gate_slice',
      'metric',
      'schema_version',
      'slices',
    ]) &&
    value.schema_version === 'release-decision-case/v1' &&
    isComparisonValue(value.baseline) &&
    (value.baseline_passed === undefined ||
      value.baseline_passed === null ||
      typeof value.baseline_passed === 'boolean') &&
    isComparisonValue(value.candidate) &&
    (value.candidate_passed === undefined ||
      value.candidate_passed === null ||
      typeof value.candidate_passed === 'boolean') &&
    isString(value.case_id) &&
    isString(value.change) &&
    CASE_CHANGES.has(value.change) &&
    isOptionalNumber(value.delta) &&
    isOptionalString(value.gate_slice) &&
    isString(value.metric) &&
    isStringArray(value.slices)
  );
}

export function isReleaseDecisionCasePage(
  value: unknown,
): value is ReleaseDecisionCasePage {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'decision_id',
      'items',
      'next_cursor',
      'schema_version',
    ]) &&
    value.schema_version === 'release-decision-case-page/v1' &&
    isString(value.decision_id) &&
    Array.isArray(value.items) &&
    value.items.every(isReleaseDecisionCase) &&
    isOptionalString(value.next_cursor)
  );
}

function isQuantileSummary(value: unknown): value is QuantileSummary {
  if (
    !isRecord(value) ||
    !hasOnlyKeys(value, [
      'maximum',
      'mean',
      'minimum',
      'p50',
      'p95',
      'sample_count',
      'small_sample',
      'suppressed',
    ]) ||
    !isCount(value.sample_count) ||
    typeof value.small_sample !== 'boolean' ||
    value.small_sample !== (value.sample_count < 20) ||
    typeof value.suppressed !== 'boolean'
  ) {
    return false;
  }
  const statistics = [
    value.minimum,
    value.p50,
    value.p95,
    value.maximum,
    value.mean,
  ];
  if (value.sample_count === 0 || value.suppressed) {
    return (
      !(value.sample_count === 0 && value.suppressed) &&
      statistics.every((item) => item === undefined || item === null)
    );
  }
  if (!statistics.every(isFiniteNumber)) return false;
  const [minimum, p50, p95, maximum, mean] = statistics;
  return (
    minimum <= p50 &&
    p50 <= p95 &&
    p95 <= maximum &&
    minimum <= mean &&
    mean <= maximum
  );
}

function isMeasurement(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'attempted',
      'measured',
      'statistics',
      'target_failures',
      'unavailable',
    ]) &&
    isCount(value.attempted) &&
    isCount(value.measured) &&
    isCount(value.target_failures) &&
    isCount(value.unavailable) &&
    value.attempted === value.measured + value.unavailable &&
    value.target_failures <= value.attempted &&
    isQuantileSummary(value.statistics) &&
    value.statistics.sample_count === value.measured
  );
}

function isScoreValues(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'attempted',
      'errors',
      'scored',
      'skipped',
      'statistics',
    ]) &&
    isCount(value.attempted) &&
    isCount(value.errors) &&
    isCount(value.scored) &&
    isCount(value.skipped) &&
    value.attempted === value.errors + value.scored + value.skipped &&
    isQuantileSummary(value.statistics) &&
    value.statistics.sample_count === value.scored
  );
}

function isDelta(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'attempted',
      'compared',
      'incomparable',
      'statistics',
    ]) &&
    isCount(value.attempted) &&
    isCount(value.compared) &&
    isCount(value.incomparable) &&
    value.attempted === value.compared + value.incomparable &&
    isQuantileSummary(value.statistics) &&
    value.statistics.sample_count === value.compared
  );
}

function isScoreDistribution(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'baseline',
      'candidate',
      'delta',
      'gate_slice',
      'metric',
    ]) &&
    isScoreValues(value.baseline) &&
    isScoreValues(value.candidate) &&
    isDelta(value.delta) &&
    isOptionalString(value.gate_slice) &&
    isString(value.metric)
  );
}

function isOperationalDistribution(
  value: unknown,
  role: 'baseline' | 'candidate',
): boolean {
  if (
    !isRecord(value) ||
    !hasOnlyKeys(value, [
      'execution_mode',
      'input_units',
      'latency_ms',
      'output_units',
      'role',
      'run_id',
      'simulated',
      'total_units',
    ]) ||
    value.role !== role ||
    !isString(value.execution_mode) ||
    !EXECUTION_MODES.has(value.execution_mode) ||
    typeof value.simulated !== 'boolean' ||
    value.simulated !== (value.execution_mode !== 'live') ||
    !isString(value.run_id)
  ) {
    return false;
  }
  return (
    isMeasurement(value.latency_ms) &&
    isMeasurement(value.input_units) &&
    isMeasurement(value.output_units) &&
    isMeasurement(value.total_units)
  );
}

export function isReleaseDecisionDistributions(
  value: unknown,
): value is ReleaseDecisionDistributions {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, [
      'baseline',
      'candidate',
      'decision_id',
      'schema_version',
      'score',
    ]) &&
    value.schema_version === 'release-decision-distributions/v1' &&
    isString(value.decision_id) &&
    isScoreDistribution(value.score) &&
    isOperationalDistribution(value.baseline, 'baseline') &&
    isOperationalDistribution(value.candidate, 'candidate')
  );
}
