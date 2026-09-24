import type { ComparisonJob, ComparisonSelection } from '../api/suite-comparison-client';
import type { ReleaseDecision } from '../api/client';
import { suitePage, suiteRunPage } from './suite-history';
import { releaseDecision } from './release-evidence';

export const selection: ComparisonSelection = {
  suite: suitePage.items[0],
  baseline: {
    ...suiteRunPage.items[0], run_id: 'suite-run-001',
    created_at: '2026-09-15T12:09:00Z', result_digest: `sha256:${'1'.repeat(64)}`,
    target: { ...suiteRunPage.items[0].target, name: 'fake/baseline', revision: 1 },
  },
  candidate: suiteRunPage.items[0],
};
export const comparisonJob: ComparisonJob = {
  schema_version: 'job/v2', job_id: 'job-comparison-001', kind: 'comparison',
  status: 'queued', resource_id: 'suite-decision-001', attempt_count: 0, max_attempts: 3,
  available_at: '2026-09-15T12:20:00Z', created_at: '2026-09-15T12:20:00Z',
  updated_at: '2026-09-15T12:20:00Z', error_code: null,
};
export const comparisonDecision: ReleaseDecision = {
  ...releaseDecision, decision_id: comparisonJob.resource_id,
  execution_mode: selection.suite.execution_mode, suite: selection.baseline.suite,
  baseline_run_id: selection.baseline.run_id, candidate_run_id: selection.candidate.run_id,
  baseline: selection.baseline.target, candidate: selection.candidate.target,
  baseline_result_digest: selection.baseline.result_digest, candidate_result_digest: selection.candidate.result_digest,
  dataset: { ...releaseDecision.dataset, name: selection.suite.dataset_name, revision: selection.suite.dataset_revision },
};
