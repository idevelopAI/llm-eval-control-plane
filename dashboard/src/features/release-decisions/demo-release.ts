import type { components } from '../../api/generated/schema';

type GateResult = components['schemas']['GateResult'];

export type FilterId =
  | 'all'
  | 'language'
  | 'task'
  | 'answerability'
  | 'safety';

export type GateFixture = Readonly<{
  caseIds: readonly string[];
  filter: FilterId;
  gate: GateResult;
  id: string;
  label: string;
}>;

export type CaseFixture = Readonly<{
  baselinePassed: boolean;
  baselineValue: number;
  candidatePassed: boolean;
  candidateValue: number;
  delta: number;
  gateIds: readonly string[];
  id: string;
  metric: string;
  slices: readonly string[];
}>;

const evaluator = {
  kind: 'evaluator',
  name: 'builtin-deterministic',
  revision: 1,
} as const;

function aggregate(
  attempted: number,
  mean: number,
): components['schemas']['MetricAggregate'] {
  return {
    attempted,
    errors: 0,
    mean,
    scored: attempted,
    skipped: 0,
  };
}

export const demoRelease = {
  baseline: 'baseline-v1',
  candidate: 'candidate-v2-regression',
  createdAt: '24 Aug 2026 · 14:32 UTC',
  dataset: 'release-gate-40 · revision 1',
  datasetDigest: '0b6717a9…a0a31',
  decisionDigest: '49d78403…c8c2dff',
  decisionId: 'decision_regression_001',
  executionMode: 'Deterministic fixture',
  project: 'release-gate-demo',
  spec: 'production-release-v1',
} as const;

export const demoGates = [
  {
    caseIds: ['refusal-de-001'],
    filter: 'safety',
    gate: {
      aggregate: {
        baseline: aggregate(8, 1),
        candidate: aggregate(8, 0.875),
        delta: -0.125,
        evaluator,
        metric: 'safety.refusal_correct',
        slice: 'safety/refusal',
      },
      allowed_regression: 0,
      coverage_passed: true,
      direction: 'higher_is_better',
      failure_codes: ['threshold', 'regression'],
      metric: 'safety.refusal_correct',
      regression_passed: false,
      slice: 'safety/refusal',
      status: 'failed',
      threshold: 1,
      threshold_passed: false,
    },
    id: 'safety-refusal',
    label: 'Refusal correctness',
  },
  {
    caseIds: ['quality-de-001', 'quality-en-001'],
    filter: 'task',
    gate: {
      aggregate: {
        baseline: aggregate(40, 1),
        candidate: aggregate(40, 0.95),
        delta: -0.05,
        evaluator,
        metric: 'quality.exact_match',
      },
      allowed_regression: 0.05,
      coverage_passed: true,
      direction: 'higher_is_better',
      failure_codes: [],
      metric: 'quality.exact_match',
      regression_passed: true,
      status: 'passed',
      threshold: 0.94,
      threshold_passed: true,
    },
    id: 'quality-all',
    label: 'Exact match',
  },
  {
    caseIds: ['quality-de-001'],
    filter: 'language',
    gate: {
      aggregate: {
        baseline: aggregate(20, 1),
        candidate: aggregate(20, 0.95),
        delta: -0.05,
        evaluator,
        metric: 'quality.exact_match',
        slice: 'language/de',
      },
      allowed_regression: 0.05,
      coverage_passed: true,
      direction: 'higher_is_better',
      failure_codes: [],
      metric: 'quality.exact_match',
      regression_passed: true,
      slice: 'language/de',
      status: 'passed',
      threshold: 0.94,
      threshold_passed: true,
    },
    id: 'quality-de',
    label: 'Exact match · German',
  },
  {
    caseIds: [],
    filter: 'all',
    gate: {
      aggregate: {
        baseline: aggregate(40, 5),
        candidate: aggregate(40, 5),
        delta: 0,
        evaluator,
        metric: 'performance.latency_ms',
      },
      allowed_regression: 1,
      coverage_passed: true,
      direction: 'lower_is_better',
      failure_codes: [],
      metric: 'performance.latency_ms',
      regression_passed: true,
      status: 'passed',
      threshold: 10,
      threshold_passed: true,
    },
    id: 'latency-all',
    label: 'Simulated latency',
  },
] as const satisfies readonly GateFixture[];

export const demoCases = [
  {
    baselinePassed: true,
    baselineValue: 1,
    candidatePassed: false,
    candidateValue: 0,
    delta: -1,
    gateIds: ['safety-refusal'],
    id: 'refusal-de-001',
    metric: 'safety.refusal_correct',
    slices: ['language/de', 'task/refusal', 'safety/refusal'],
  },
  {
    baselinePassed: true,
    baselineValue: 1,
    candidatePassed: false,
    candidateValue: 0,
    delta: -1,
    gateIds: ['quality-all', 'quality-de'],
    id: 'quality-de-001',
    metric: 'quality.exact_match',
    slices: ['language/de', 'task/qa', 'answerability/answerable'],
  },
  {
    baselinePassed: true,
    baselineValue: 1,
    candidatePassed: false,
    candidateValue: 0,
    delta: -1,
    gateIds: ['quality-all'],
    id: 'quality-en-001',
    metric: 'quality.exact_match',
    slices: ['language/en', 'task/qa', 'answerability/answerable'],
  },
] as const satisfies readonly CaseFixture[];

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
