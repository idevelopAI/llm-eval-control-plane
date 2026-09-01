import type { ReleaseDecisionDistributions } from '../../api/client';
import type {
  CaseView,
  GateView,
  ReleaseDashboardModel,
  ReleaseView,
} from './view-model';
import {
  PUBLIC_FIXTURE_CASE_ID_PREFIX,
  PUBLIC_FIXTURE_DECISION_ID_PREFIX,
  PUBLIC_FIXTURE_RUN_ID_PREFIX,
  PUBLIC_FIXTURE_SCHEMA_VERSION,
} from './public-fixture-contract';
import { gateId } from './view-model';

export { filterOptions } from './view-model';
export type { FilterId } from './view-model';

export type GateFixture = GateView;
export type CaseFixture = CaseView;

const SAFETY_GATE_ID = gateId('safety.refusal_correct', 'safety/refusal');
const QUALITY_GATE_ID = gateId('quality.exact_match', null);
const QUALITY_DE_GATE_ID = gateId('quality.exact_match', 'language/de');
const LATENCY_GATE_ID = gateId('performance.latency_ms', null);

const evaluator = {
  kind: 'evaluator',
  name: 'builtin-deterministic',
  revision: 1,
} as const;

function aggregate(attempted: number, mean: number) {
  return { attempted, errors: 0, mean, scored: attempted, skipped: 0 };
}

export const demoRelease: ReleaseView = {
  baseline: 'baseline-v1 · revision 1',
  candidate: 'candidate-v2-regression · revision 2',
  createdAt: '24 Aug 2026 · 14:32 UTC',
  dataset: 'release-gate-40 · revision 1',
  datasetDigest: '0b6717a9…a0a31',
  decisionDigest: '49d78403…c8c2dff',
  decisionId: `${PUBLIC_FIXTURE_DECISION_ID_PREFIX}regression-001`,
  executionMode: 'Offline deterministic evaluation',
  project: 'public-example',
  simulated: true,
  spec: 'synthetic-release-policy-v1',
  status: 'failed',
};

export const demoGates = [
  {
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
    id: SAFETY_GATE_ID,
    label: 'Refusal correctness',
  },
  {
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
    id: QUALITY_GATE_ID,
    label: 'Exact match',
  },
  {
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
    id: QUALITY_DE_GATE_ID,
    label: 'Exact match · German',
  },
  {
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
    id: LATENCY_GATE_ID,
    label: 'Simulated latency',
  },
] as const satisfies readonly GateFixture[];

export const demoCases = [
  {
    baselinePassed: true,
    baselineValue: 1,
    candidatePassed: false,
    candidateValue: 0,
    change: 'newly_failing',
    delta: -1,
    gateIds: [SAFETY_GATE_ID],
    id: `${PUBLIC_FIXTURE_CASE_ID_PREFIX}refusal-de-001`,
    metric: 'safety.refusal_correct',
    slices: ['language/de', 'task/refusal', 'safety/refusal'],
  },
  {
    baselinePassed: true,
    baselineValue: 1,
    candidatePassed: false,
    candidateValue: 0,
    change: 'newly_failing',
    delta: -1,
    gateIds: [QUALITY_GATE_ID, QUALITY_DE_GATE_ID],
    id: `${PUBLIC_FIXTURE_CASE_ID_PREFIX}quality-de-001`,
    metric: 'quality.exact_match',
    slices: ['language/de', 'task/qa', 'answerability/answerable'],
  },
  {
    baselinePassed: true,
    baselineValue: 1,
    candidatePassed: false,
    candidateValue: 0,
    change: 'newly_failing',
    delta: -1,
    gateIds: [QUALITY_GATE_ID],
    id: `${PUBLIC_FIXTURE_CASE_ID_PREFIX}quality-en-001`,
    metric: 'quality.exact_match',
    slices: ['language/en', 'task/qa', 'answerability/answerable'],
  },
] as const satisfies readonly CaseFixture[];

function quantiles(
  sampleCount: number,
  value: number | null,
  options: { suppressSmall: boolean },
) {
  const suppressed =
    options.suppressSmall && sampleCount > 0 && sampleCount < 20;
  const exposed = sampleCount > 0 && !suppressed ? value : null;
  return {
    maximum: exposed,
    mean: exposed,
    minimum: exposed,
    p50: exposed,
    p95: exposed,
    sample_count: sampleCount,
    small_sample: sampleCount < 20,
    suppressed,
  };
}

function demoDistributions(gate: GateFixture): ReleaseDecisionDistributions {
  const { baseline, candidate, delta } = gate.gate.aggregate;
  const measurement = (attempted: number, value: number) => ({
    attempted,
    measured: attempted,
    statistics: quantiles(attempted, value, { suppressSmall: true }),
    target_failures: 0,
    unavailable: 0,
  });
  const run = (role: 'baseline' | 'candidate', valueOffset: number) => ({
    execution_mode: 'offline_deterministic_fixture' as const,
    input_units: measurement(baseline.attempted, 18 + valueOffset),
    latency_ms: measurement(baseline.attempted, 5 + valueOffset),
    output_units: measurement(baseline.attempted, 7 + valueOffset),
    role,
    run_id: `${PUBLIC_FIXTURE_RUN_ID_PREFIX}${role}-001`,
    simulated: true,
    total_units: measurement(baseline.attempted, 25 + valueOffset),
  });
  const score = (value: typeof baseline) => ({
    attempted: value.attempted,
    errors: value.errors,
    scored: value.scored,
    skipped: value.skipped,
    statistics: quantiles(value.scored, value.mean ?? null, {
      suppressSmall: false,
    }),
  });
  const compared = delta == null ? 0 : Math.min(baseline.scored, candidate.scored);
  return {
    baseline: run('baseline', 0),
    candidate: run('candidate', 1),
    decision_id: demoRelease.decisionId,
    schema_version: 'release-decision-distributions/v1',
    score: {
      baseline: score(baseline),
      candidate: score(candidate),
      delta: {
        attempted: baseline.attempted,
        compared,
        incomparable: baseline.attempted - compared,
        statistics: quantiles(compared, delta ?? null, {
          suppressSmall: false,
        }),
      },
      gate_slice: gate.gate.slice ?? null,
      metric: gate.gate.metric,
    },
  };
}

export const publicSyntheticFixture = {
  cases: demoCases,
  distributions: demoGates.map((gate) => demoDistributions(gate)),
  gates: demoGates,
  release: demoRelease,
  schemaVersion: PUBLIC_FIXTURE_SCHEMA_VERSION,
} as const;

// The safety contract and pinned canonical digest cover this exact payload, and
// every public dashboard interaction below reads directly from it.

export function demoModelForGate(gateId: string): ReleaseDashboardModel {
  const selectedGateIndex = demoGates.findIndex((gate) => gate.id === gateId);
  const selectedGate = demoGates[selectedGateIndex];
  if (!selectedGate) throw new Error('Fixture gate is unavailable.');
  return {
    casePageTruncated: false,
    cases: publicSyntheticFixture.cases,
    distributions: publicSyntheticFixture.distributions[selectedGateIndex],
    gates: publicSyntheticFixture.gates,
    release: publicSyntheticFixture.release,
    selectedGateId: selectedGate.id,
  };
}

export const demoDashboardModel = demoModelForGate(demoGates[0].id);
