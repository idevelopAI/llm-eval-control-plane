import { comparisonJob } from './suite-comparison';
import { suitePage, suiteRunPage } from './suite-history';
import type { LocalJob } from '../api/local-job-client';
import type { SuiteRunSelection } from '../api/suite-run-client';

export const runSelection: SuiteRunSelection = { suite: suitePage.items[0], targetId: 'candidate' };
export const runJob: LocalJob = {
  ...comparisonJob, kind: 'run', job_id: 'job-run-001', resource_id: suiteRunPage.items[0].run_id,
};
export const runSummary = {
  schema_version: 'run-summary/v1',
  run_id: runJob.resource_id, status: 'completed', execution_mode: 'offline_mock',
  dataset: { kind: 'dataset', name: runSelection.suite.dataset_name, revision: 1, digest: `sha256:${'d'.repeat(64)}` },
  target: suiteRunPage.items[0].target, suite: suiteRunPage.items[0].suite,
  result_digest: suiteRunPage.items[0].result_digest, created_at: suiteRunPage.items[0].created_at,
  evaluators: [], metrics: [], case_status_counts: { completed: 1, completed_with_errors: 0, target_failed: 0 },
};
