import type {
  ReleaseDecision,
  ReleaseDecisionCasePage,
  ReleaseDecisionDistributions,
} from '../../api/client';

export type FilterId =
  | 'all'
  | 'language'
  | 'task'
  | 'answerability'
  | 'safety';

export type GateView = Readonly<{
  filter: FilterId;
  gate: ReleaseDecision['gates'][number];
  id: string;
  label: string;
}>;

export type CaseView = Readonly<{
  baselinePassed: boolean | null;
  baselineValue: number | null;
  candidatePassed: boolean | null;
  candidateValue: number | null;
  change: ReleaseDecisionCasePage['items'][number]['change'];
  delta: number | null;
  gateIds: readonly string[];
  id: string;
  metric: string;
  slices: readonly string[];
}>;

export type ReleaseView = Readonly<{
  baseline: string;
  candidate: string;
  createdAt: string;
  dataset: string;
  datasetDigest: string;
  decisionDigest: string;
  decisionId: string;
  executionMode: string;
  project: string;
  simulated: boolean;
  spec: string;
  status: 'passed' | 'failed';
}>;

export type ReleaseDashboardModel = Readonly<{
  casePageTruncated: boolean;
  cases: readonly CaseView[] | null;
  distributions: ReleaseDecisionDistributions | null;
  gates: readonly GateView[];
  release: ReleaseView;
  selectedGateId: string;
}>;

const FILTER_IDS = new Set<FilterId>([
  'language',
  'task',
  'answerability',
  'safety',
]);

export const filterOptions: readonly Readonly<{
  id: FilterId;
  label: string;
}>[] = [
  { id: 'all', label: 'All gates' },
  { id: 'language', label: 'Language' },
  { id: 'task', label: 'Task' },
  { id: 'answerability', label: 'Answerability' },
  { id: 'safety', label: 'Safety' },
];

export function gateId(metric: string, slice: string | null | undefined): string {
  return JSON.stringify([metric, slice ?? null]);
}

function gateFilter(slice: string | null | undefined): FilterId {
  const family = slice?.split('/', 1)[0];
  return family && FILTER_IDS.has(family as FilterId)
    ? (family as FilterId)
    : 'all';
}

function gateLabel(metric: string, slice: string | null | undefined): string {
  const metricName = metric.split(/[.:/]/).at(-1) ?? metric;
  const words = metricName.replaceAll('_', ' ');
  const label = `${words.charAt(0).toUpperCase()}${words.slice(1)}`;
  if (!slice) return label;
  const sliceName = slice.split('/').at(-1)?.replaceAll('_', ' ') ?? slice;
  return `${label} · ${sliceName}`;
}

function artifactLabel(artifact: ReleaseDecision['dataset']): string {
  return `${artifact.name} · revision ${artifact.revision}`;
}

function abbreviatedDigest(value: string | null | undefined): string {
  if (!value) return 'not exposed';
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-7)}` : value;
}

function utcTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return 'timestamp unavailable';
  const parts = new Intl.DateTimeFormat('en-GB', {
    day: '2-digit',
    hour: '2-digit',
    hour12: false,
    minute: '2-digit',
    month: 'short',
    timeZone: 'UTC',
    year: 'numeric',
  }).format(date);
  return `${parts} UTC`;
}

function executionModeLabel(mode: ReleaseDecision['execution_mode']): string {
  if (mode === 'live') return 'Live execution';
  if (mode === 'offline_mock') return 'Offline mock';
  return 'Offline deterministic evaluation';
}

function inconsistentEvidence(): never {
  throw new Error('Release dashboard evidence is inconsistent.');
}

function numbersEqual(
  left: number | null | undefined,
  right: number | null | undefined,
): boolean {
  if (left == null || right == null) return left == null && right == null;
  const scale = Math.max(1, Math.abs(left), Math.abs(right));
  return Math.abs(left - right) <= Number.EPSILON * scale * 8;
}

function aggregateIsInternallyConsistent(
  aggregate: ReleaseDecision['aggregates'][number],
): boolean {
  const baselineCount =
    aggregate.baseline.scored +
    aggregate.baseline.skipped +
    aggregate.baseline.errors;
  const candidateCount =
    aggregate.candidate.scored +
    aggregate.candidate.skipped +
    aggregate.candidate.errors;
  return (
    aggregate.baseline.attempted === baselineCount &&
    aggregate.candidate.attempted === candidateCount &&
    aggregate.baseline.attempted === aggregate.candidate.attempted &&
    (aggregate.baseline.scored > 0) === (aggregate.baseline.mean != null) &&
    (aggregate.candidate.scored > 0) === (aggregate.candidate.mean != null) &&
    numbersEqual(
      aggregate.delta,
      aggregate.baseline.mean == null || aggregate.candidate.mean == null
        ? null
        : aggregate.candidate.mean - aggregate.baseline.mean,
    )
  );
}

function aggregatesMatch(
  left: ReleaseDecision['aggregates'][number],
  right: ReleaseDecision['aggregates'][number],
): boolean {
  return (
    left.metric === right.metric &&
    (left.slice ?? null) === (right.slice ?? null) &&
    left.evaluator.kind === right.evaluator.kind &&
    left.evaluator.name === right.evaluator.name &&
    left.evaluator.revision === right.evaluator.revision &&
    left.evaluator.digest === right.evaluator.digest &&
    left.baseline.attempted === right.baseline.attempted &&
    left.baseline.scored === right.baseline.scored &&
    left.baseline.skipped === right.baseline.skipped &&
    left.baseline.errors === right.baseline.errors &&
    numbersEqual(left.baseline.mean, right.baseline.mean) &&
    left.candidate.attempted === right.candidate.attempted &&
    left.candidate.scored === right.candidate.scored &&
    left.candidate.skipped === right.candidate.skipped &&
    left.candidate.errors === right.candidate.errors &&
    numbersEqual(left.candidate.mean, right.candidate.mean) &&
    numbersEqual(left.delta, right.delta)
  );
}

function gateIsConsistent(gate: ReleaseDecision['gates'][number]): boolean {
  const expectedFailures = [
    !gate.coverage_passed ? 'coverage' : null,
    !gate.threshold_passed ? 'threshold' : null,
    !gate.regression_passed ? 'regression' : null,
  ].filter((value): value is 'coverage' | 'threshold' | 'regression' => value != null);
  return (
    aggregateIsInternallyConsistent(gate.aggregate) &&
    gate.metric === gate.aggregate.metric &&
    (gate.slice ?? null) === (gate.aggregate.slice ?? null) &&
    (gate.status === 'failed') === (expectedFailures.length > 0) &&
    expectedFailures.length === gate.failure_codes.length &&
    expectedFailures.every((code) => gate.failure_codes.includes(code))
  );
}

function caseIsConsistent(
  item: ReleaseDecisionCasePage['items'][number],
): boolean {
  const baselineScored = item.baseline.status === 'scored';
  const candidateScored = item.candidate.status === 'scored';
  const comparable = baselineScored && candidateScored;
  const baselinePassed = item.baseline_passed ?? null;
  const candidatePassed = item.candidate_passed ?? null;
  if (
    comparable !== (baselinePassed != null && candidatePassed != null) ||
    comparable !== (item.delta != null) ||
    (!comparable && (baselinePassed != null || candidatePassed != null))
  ) {
    return false;
  }
  if (
    comparable &&
    (item.baseline.value == null ||
      item.candidate.value == null ||
      !numbersEqual(item.delta, item.candidate.value - item.baseline.value))
  ) {
    return false;
  }
  const expectedChange =
    baselinePassed == null || candidatePassed == null
      ? 'incomparable'
      : baselinePassed && !candidatePassed
        ? 'newly_failing'
        : !baselinePassed && candidatePassed
          ? 'newly_passing'
          : baselinePassed
            ? 'unchanged_passing'
            : 'unchanged_failing';
  return item.change === expectedChange;
}

function distributionMatchesGate(
  distributions: ReleaseDecisionDistributions,
  gate: ReleaseDecision['gates'][number],
): boolean {
  const { aggregate } = gate;
  const { baseline, candidate, delta } = distributions.score;
  const attempted = aggregate.baseline.attempted;
  const operationalAttempts = [
    distributions.baseline.latency_ms.attempted,
    distributions.baseline.input_units.attempted,
    distributions.baseline.output_units.attempted,
    distributions.baseline.total_units.attempted,
    distributions.candidate.latency_ms.attempted,
    distributions.candidate.input_units.attempted,
    distributions.candidate.output_units.attempted,
    distributions.candidate.total_units.attempted,
  ];
  return (
    distributions.score.metric === gate.metric &&
    (distributions.score.gate_slice ?? null) === (gate.slice ?? null) &&
    baseline.attempted === aggregate.baseline.attempted &&
    baseline.scored === aggregate.baseline.scored &&
    baseline.skipped === aggregate.baseline.skipped &&
    baseline.errors === aggregate.baseline.errors &&
    numbersEqual(baseline.statistics.mean, aggregate.baseline.mean) &&
    candidate.attempted === aggregate.candidate.attempted &&
    candidate.scored === aggregate.candidate.scored &&
    candidate.skipped === aggregate.candidate.skipped &&
    candidate.errors === aggregate.candidate.errors &&
    numbersEqual(candidate.statistics.mean, aggregate.candidate.mean) &&
    delta.attempted === attempted &&
    operationalAttempts.every((value) => value === attempted)
  );
}

export function buildReleaseDashboardModel({
  cases,
  decision,
  distributions,
  projectId,
  selectedGate: requestedGate,
}: {
  cases: ReleaseDecisionCasePage | null;
  decision: ReleaseDecision;
  distributions: ReleaseDecisionDistributions | null;
  projectId: string;
  selectedGate?: Readonly<{ metric: string; slice: string | null }>;
}): ReleaseDashboardModel {
  const uniqueGateIds = new Set(
    decision.gates.map((gate) => gateId(gate.metric, gate.slice)),
  );
  const expectedDecisionStatus = decision.gates.some(
    (gate) => gate.status === 'failed',
  )
    ? 'failed'
    : 'passed';
  if (
    decision.gates.length === 0 ||
    (cases == null && distributions == null) ||
    uniqueGateIds.size !== decision.gates.length ||
    decision.status !== expectedDecisionStatus ||
    decision.aggregates.some((aggregate) => !aggregateIsInternallyConsistent(aggregate)) ||
    decision.gates.some((gate) => !gateIsConsistent(gate)) ||
    decision.gates.some(
      (gate) =>
        !decision.aggregates.some((aggregate) =>
          aggregatesMatch(gate.aggregate, aggregate),
        ),
    ) ||
    (cases != null && cases.decision_id !== decision.decision_id) ||
    (distributions != null &&
      (distributions.decision_id !== decision.decision_id ||
        distributions.baseline.run_id !== decision.baseline_run_id ||
        distributions.candidate.run_id !== decision.candidate_run_id ||
        distributions.baseline.execution_mode !== decision.execution_mode ||
        distributions.candidate.execution_mode !== decision.execution_mode ||
        (cases != null &&
          cases.items.length > distributions.score.delta.attempted)))
  ) {
    return inconsistentEvidence();
  }

  const selectedMetric = requestedGate?.metric ?? distributions?.score.metric;
  const selectedSlice = requestedGate
    ? requestedGate.slice
    : (distributions?.score.gate_slice ?? null);
  if (!selectedMetric) return inconsistentEvidence();
  const selectedGate = decision.gates.find(
    (gate) =>
      gate.metric === selectedMetric && (gate.slice ?? null) === selectedSlice,
  );
  if (!selectedGate) return inconsistentEvidence();
  if (distributions && !distributionMatchesGate(distributions, selectedGate)) {
    return inconsistentEvidence();
  }
  if (
    cases?.items.some(
      (item) =>
        item.metric !== selectedMetric ||
        (item.gate_slice ?? null) !== selectedSlice ||
        !caseIsConsistent(item),
    )
  ) {
    return inconsistentEvidence();
  }

  return {
    casePageTruncated: cases?.next_cursor != null,
    cases:
      cases?.items.map((item) => ({
        baselinePassed: item.baseline_passed ?? null,
        baselineValue: item.baseline.value ?? null,
        candidatePassed: item.candidate_passed ?? null,
        candidateValue: item.candidate.value ?? null,
        change: item.change,
        delta: item.delta ?? null,
        gateIds: [gateId(selectedMetric, selectedSlice)],
        id: item.case_id,
        metric: item.metric,
        slices: item.slices,
      })) ?? null,
    distributions,
    gates: decision.gates
      .map((gate, index) => ({
        index,
        view: {
          filter: gateFilter(gate.slice),
          gate,
          id: gateId(gate.metric, gate.slice),
          label: gateLabel(gate.metric, gate.slice),
        },
      }))
      .sort(
        (left, right) =>
          Number(left.view.gate.status === 'passed') -
            Number(right.view.gate.status === 'passed') || left.index - right.index,
      )
      .map((item) => item.view),
    release: {
      baseline: artifactLabel(decision.baseline),
      candidate: artifactLabel(decision.candidate),
      createdAt: utcTimestamp(decision.created_at),
      dataset: artifactLabel(decision.dataset),
      datasetDigest: abbreviatedDigest(decision.dataset.digest),
      decisionDigest: abbreviatedDigest(decision.decision_digest),
      decisionId: decision.decision_id,
      executionMode: executionModeLabel(decision.execution_mode),
      project: projectId,
      simulated: decision.execution_mode !== 'live',
      spec: decision.spec_name,
      status: decision.status,
    },
    selectedGateId: gateId(selectedMetric, selectedSlice),
  };
}
