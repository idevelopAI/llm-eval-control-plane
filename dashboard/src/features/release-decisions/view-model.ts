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
  cases: readonly CaseView[];
  distributions: ReleaseDecisionDistributions;
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
  return 'Deterministic fixture';
}

function inconsistentEvidence(): never {
  throw new Error('Release dashboard evidence is inconsistent.');
}

function gateIsConsistent(gate: ReleaseDecision['gates'][number]): boolean {
  const expectedFailures = [
    !gate.coverage_passed ? 'coverage' : null,
    !gate.threshold_passed ? 'threshold' : null,
    !gate.regression_passed ? 'regression' : null,
  ].filter((value): value is 'coverage' | 'threshold' | 'regression' => value != null);
  return (
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
  const baselinePassed = item.baseline_passed ?? null;
  const candidatePassed = item.candidate_passed ?? null;
  if (
    baselineScored !== (baselinePassed != null) ||
    candidateScored !== (candidatePassed != null) ||
    (baselineScored && candidateScored) !== (item.delta != null)
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

export function buildReleaseDashboardModel({
  cases,
  decision,
  distributions,
  projectId,
}: {
  cases: ReleaseDecisionCasePage;
  decision: ReleaseDecision;
  distributions: ReleaseDecisionDistributions;
  projectId: string;
}): ReleaseDashboardModel {
  if (
    decision.gates.length === 0 ||
    decision.gates.some((gate) => !gateIsConsistent(gate)) ||
    cases.decision_id !== decision.decision_id ||
    distributions.decision_id !== decision.decision_id ||
    distributions.baseline.run_id !== decision.baseline_run_id ||
    distributions.candidate.run_id !== decision.candidate_run_id ||
    distributions.baseline.execution_mode !== decision.execution_mode ||
    distributions.candidate.execution_mode !== decision.execution_mode
  ) {
    return inconsistentEvidence();
  }

  const selectedMetric = distributions.score.metric;
  const selectedSlice = distributions.score.gate_slice ?? null;
  const selectedGate = decision.gates.find(
    (gate) =>
      gate.metric === selectedMetric && (gate.slice ?? null) === selectedSlice,
  );
  if (!selectedGate) return inconsistentEvidence();
  if (
    cases.items.some(
      (item) =>
        item.metric !== selectedMetric ||
        (item.gate_slice ?? null) !== selectedSlice ||
        !caseIsConsistent(item),
    )
  ) {
    return inconsistentEvidence();
  }

  return {
    casePageTruncated: cases.next_cursor != null,
    cases: cases.items.map((item) => ({
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
    })),
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
