import type { components } from './generated/schema';
import { createLocalJobClient, isLocalJob, keys, record, localJobError as failure } from './local-job-client';
import { isReleaseDecision } from './validation';
import { isSuiteRunHistoryPage, sameSuitePin, sameTargetPin } from './suite-history-validation';
import type { CredentialSource } from '../security/runtime-credential-vault';

export type SuiteRun = components['schemas']['SuiteRunHistoryItemResponse'];
export type ComparisonSuite = components['schemas']['SuiteListItemResponse'];
export type ComparisonJob = components['schemas']['JobResponse'];
type Decision = components['schemas']['ReleaseDecisionResponse'];
export type ComparisonSelection = Readonly<{
  suite: ComparisonSuite;
  baseline: SuiteRun;
  candidate: SuiteRun;
}>;
export function comparisonIssue(selection: ComparisonSelection): string | null {
  const { suite, baseline, candidate } = selection;
  if (baseline.run_id === candidate.run_id) return 'Choose two different runs.';
  const pin = { kind: 'suite', name: suite.name, revision: suite.revision, digest: suite.digest } as const;
  for (const run of [baseline, candidate]) {
    if (!isSuiteRunHistoryPage({ schema_version: 'suite-run-history-page/v1', items: [run] }) ||
      !sameSuitePin(run.suite, pin) || run.dataset_name !== suite.dataset_name ||
      run.dataset_revision !== suite.dataset_revision || run.execution_mode !== suite.execution_mode) {
      return 'Both runs must match the exact suite, dataset revision, and execution mode.';
    }
  }
  // The local API currently implements only this credential-free executor.
  if (suite.execution_mode !== 'offline_mock') return 'This local workflow supports deterministic offline suites only.';
  return null;
}

export function isComparisonJob(value: unknown): value is ComparisonJob {
  return isLocalJob(value, 'comparison');
}

export function matchesComparisonDecision(decision: Decision, selection: ComparisonSelection, job: ComparisonJob): boolean {
  return job.status === 'succeeded' && isReleaseDecision(decision) && decision.decision_id === job.resource_id &&
    decision.baseline_run_id === selection.baseline.run_id && decision.candidate_run_id === selection.candidate.run_id &&
    decision.baseline_result_digest === selection.baseline.result_digest &&
    decision.candidate_result_digest === selection.candidate.result_digest &&
    decision.dataset.name === selection.suite.dataset_name && decision.dataset.revision === selection.suite.dataset_revision &&
    decision.suite != null && sameSuitePin(decision.suite, selection.baseline.suite) &&
    decision.execution_mode === selection.suite.execution_mode &&
    sameTargetPin(decision.baseline, selection.baseline.target) &&
    sameTargetPin(decision.candidate, selection.candidate.target);
}

/** Separate, explicit comparison surface; run submission uses its own client. */
export function createSuiteComparisonClient(readCredential: CredentialSource) {
  const jobs = createLocalJobClient(readCredential, 'comparison');
  return {
    async submit(selection: ComparisonSelection, key: string, writeCredential: CredentialSource, signal: AbortSignal): Promise<ComparisonJob> {
      if (comparisonIssue(selection) || !/^comparison-[a-f0-9-]{36}$/.test(key)) throw failure(400, 'invalid_submission');
      const value = await jobs.submit({
        suite_name: selection.suite.name, suite_revision: selection.suite.revision,
        baseline_run_id: selection.baseline.run_id, candidate_run_id: selection.candidate.run_id,
      } satisfies components['schemas']['SuiteComparisonCreateRequest'], key, writeCredential, signal);
      if (!record(value) || !keys(value, ['schema_version', 'job', 'decision']) ||
          value.schema_version !== 'comparison-submission/v2' || !isComparisonJob(value.job)) throw failure(200);
      if (value.decision != null && (value.job.status !== 'succeeded' ||
          !isReleaseDecision(value.decision) || !matchesComparisonDecision(value.decision, selection, value.job))) throw failure(200);
      return value.job;
    },
    getJob: jobs.getJob,
  };
}
export type SuiteComparisonClient = ReturnType<typeof createSuiteComparisonClient>;
