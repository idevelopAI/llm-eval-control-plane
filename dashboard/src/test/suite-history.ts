import type { components } from '../api/generated/schema';

export const suitePin = {
  digest: `sha256:${'a'.repeat(64)}`,
  kind: 'suite',
  name: 'release/core',
  revision: 1,
} as const;

export const suitePage = {
  schema_version: 'suite-page/v1',
  items: [
    {
      schema_version: 'suite-list-item/v1',
      name: suitePin.name,
      revision: suitePin.revision,
      digest: suitePin.digest,
      dataset_name: 'release-gate/offline',
      dataset_revision: 1,
      evaluator_count: 1,
      metric_count: 1,
      slice_count: 1,
      gate_count: 1,
      execution_mode: 'offline_mock',
      created_at: '2026-09-15T12:00:00Z',
    },
  ],
  next_cursor: null,
} satisfies components['schemas']['SuitePage'];

export const suiteRunPage = {
  schema_version: 'suite-run-history-page/v1',
  items: [
    {
      schema_version: 'suite-run-history-item/v1',
      run_id: 'suite-run-002',
      status: 'completed',
      execution_mode: 'offline_mock',
      dataset_name: 'release-gate/offline',
      dataset_revision: 1,
      result_digest: `sha256:${'b'.repeat(64)}`,
      created_at: '2026-09-15T12:10:00Z',
      suite: suitePin,
      target: {
        kind: 'target',
        name: 'fake/candidate',
        revision: 2,
        digest: `sha256:${'c'.repeat(64)}`,
      },
    },
  ],
  next_cursor: null,
} satisfies components['schemas']['SuiteRunHistoryPage'];

export const suiteDecisionPage = {
  schema_version: 'suite-decision-history-page/v1',
  items: [
    {
      schema_version: 'suite-decision-history-item/v1',
      decision_id: 'suite-decision-001',
      status: 'failed',
      baseline_run_id: 'suite-run-001',
      candidate_run_id: 'suite-run-002',
      decision_digest: `sha256:${'d'.repeat(64)}`,
      created_at: '2026-09-15T12:20:00Z',
      suite: suitePin,
    },
  ],
  next_cursor: null,
} satisfies components['schemas']['SuiteDecisionHistoryPage'];
