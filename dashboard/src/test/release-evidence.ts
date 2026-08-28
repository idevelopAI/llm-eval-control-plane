import type {
  ReleaseDecision,
  ReleaseDecisionCasePage,
  ReleaseDecisionDistributions,
  ReleaseDecisionPage,
} from '../api/client';

const evaluator = {
  digest: `sha256:${'1'.repeat(64)}`,
  kind: 'evaluator',
  name: 'builtin/exact-match',
  revision: 1,
} as const;

const aggregate = {
  baseline: { attempted: 1, errors: 0, mean: 1, scored: 1, skipped: 0 },
  candidate: { attempted: 1, errors: 0, mean: 0, scored: 1, skipped: 0 },
  delta: -1,
  evaluator,
  metric: 'quality.exact_match',
  slice: 'language/de',
} as const;

export const releaseDecision: ReleaseDecision = {
  aggregates: [aggregate],
  baseline: {
    digest: `sha256:${'2'.repeat(64)}`,
    kind: 'target',
    name: 'models/baseline',
    revision: 1,
  },
  baseline_result_digest: `sha256:${'3'.repeat(64)}`,
  baseline_run_id: 'run-baseline',
  candidate: {
    digest: `sha256:${'4'.repeat(64)}`,
    kind: 'target',
    name: 'models/candidate',
    revision: 2,
  },
  candidate_result_digest: `sha256:${'5'.repeat(64)}`,
  candidate_run_id: 'run-candidate',
  created_at: '2026-08-27T14:32:00Z',
  dataset: {
    digest: `sha256:${'6'.repeat(64)}`,
    kind: 'dataset',
    name: 'release-gate',
    revision: 3,
  },
  decision_digest: `sha256:${'7'.repeat(64)}`,
  decision_id: 'decision-001',
  execution_mode: 'offline_mock',
  gates: [
    {
      aggregate,
      allowed_regression: 0,
      coverage_passed: true,
      direction: 'higher_is_better',
      failure_codes: ['threshold', 'regression'],
      metric: 'quality.exact_match',
      regression_passed: false,
      slice: 'language/de',
      status: 'failed',
      threshold: 1,
      threshold_passed: false,
    },
  ],
  schema_version: 'release-decision-summary/v1',
  spec_name: 'production-release',
  status: 'failed',
};

export const releaseDecisionPage: ReleaseDecisionPage = {
  items: [
    {
      baseline_run_id: releaseDecision.baseline_run_id,
      candidate_run_id: releaseDecision.candidate_run_id,
      created_at: releaseDecision.created_at,
      decision_digest: releaseDecision.decision_digest,
      decision_id: releaseDecision.decision_id,
      schema_version: 'release-decision-list-item/v1',
      status: releaseDecision.status,
    },
  ],
  next_cursor: null,
  schema_version: 'release-decision-page/v1',
};

export const releaseCases: ReleaseDecisionCasePage = {
  decision_id: 'decision-001',
  items: [
    {
      baseline: { status: 'scored', value: 1 },
      baseline_passed: true,
      candidate: { status: 'scored', value: 0 },
      candidate_passed: false,
      case_id: 'case-001',
      change: 'newly_failing',
      delta: -1,
      gate_slice: 'language/de',
      metric: 'quality.exact_match',
      schema_version: 'release-decision-case/v1',
      slices: ['language/de'],
    },
  ],
  next_cursor: 'bounded-next-page',
  schema_version: 'release-decision-case-page/v1',
};

const observed = {
  maximum: 1,
  mean: 1,
  minimum: 1,
  p50: 1,
  p95: 1,
  sample_count: 1,
  small_sample: true,
  suppressed: false,
};

const candidateObserved = {
  ...observed,
  maximum: 0,
  mean: 0,
  minimum: 0,
  p50: 0,
  p95: 0,
};

const deltaObserved = {
  ...observed,
  maximum: -1,
  mean: -1,
  minimum: -1,
  p50: -1,
  p95: -1,
};

const measurement = {
  attempted: 1,
  measured: 1,
  statistics: {
    maximum: null,
    mean: null,
    minimum: null,
    p50: null,
    p95: null,
    sample_count: 1,
    small_sample: true,
    suppressed: true,
  },
  target_failures: 0,
  unavailable: 0,
};

export const releaseDistributions: ReleaseDecisionDistributions = {
  baseline: {
    execution_mode: 'offline_mock',
    input_units: measurement,
    latency_ms: measurement,
    output_units: measurement,
    role: 'baseline',
    run_id: 'run-baseline',
    simulated: true,
    total_units: measurement,
  },
  candidate: {
    execution_mode: 'offline_mock',
    input_units: measurement,
    latency_ms: measurement,
    output_units: measurement,
    role: 'candidate',
    run_id: 'run-candidate',
    simulated: true,
    total_units: measurement,
  },
  decision_id: 'decision-001',
  schema_version: 'release-decision-distributions/v1',
  score: {
    baseline: {
      attempted: 1,
      errors: 0,
      scored: 1,
      skipped: 0,
      statistics: observed,
    },
    candidate: {
      attempted: 1,
      errors: 0,
      scored: 1,
      skipped: 0,
      statistics: candidateObserved,
    },
    delta: {
      attempted: 1,
      compared: 1,
      incomparable: 0,
      statistics: deltaObserved,
    },
    gate_slice: 'language/de',
    metric: 'quality.exact_match',
  },
};
