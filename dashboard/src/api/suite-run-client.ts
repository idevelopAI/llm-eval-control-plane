import type { components } from './generated/schema';
import type { CredentialSource } from '../security/runtime-credential-vault';
import { createLocalJobClient, isLocalJob, keys, record, localJobError as failure, type LocalJob } from './local-job-client';
import { isSuitePage, isSuiteRunHistoryPage, sameSuitePin } from './suite-history-validation';

export const OFFLINE_TARGETS = [
  { id: 'baseline', name: 'fake/baseline', revision: 1 },
  { id: 'candidate', name: 'fake/candidate', revision: 2 },
] as const;
export type SuiteRunSelection = Readonly<{
  suite: components['schemas']['SuiteListItemResponse'];
  targetId: string;
}>;

export function runSelectionIssue(selection: SuiteRunSelection): string | null {
  if (!isSuitePage({ schema_version: 'suite-page/v1', items: [selection.suite] }) ||
      selection.suite.execution_mode !== 'offline_mock') {
    return 'Choose a registered deterministic offline suite.';
  }
  if (!OFFLINE_TARGETS.some((target) => target.id === selection.targetId)) return 'Choose an offline target.';
  return null;
}

// Verify only identity metadata on terminal replays; metric content is discarded.
// Completed evidence is loaded independently through validated suite history.
function matchesRun(value: unknown, selection: SuiteRunSelection, job: LocalJob): boolean {
  if (!record(value) || !keys(value, [
    'schema_version', 'run_id', 'status', 'execution_mode', 'dataset', 'target',
    'evaluators', 'case_status_counts', 'metrics', 'result_digest', 'created_at', 'suite',
  ]) || value.schema_version !== 'run-summary/v1' || !record(value.dataset)) return false;
  const item = {
    schema_version: 'suite-run-history-item/v1',
    run_id: value.run_id, status: value.status, execution_mode: value.execution_mode,
    dataset_name: value.dataset.name, dataset_revision: value.dataset.revision,
    result_digest: value.result_digest, created_at: value.created_at, suite: value.suite, target: value.target,
  };
  const page = { schema_version: 'suite-run-history-page/v1', items: [item] };
  if (!isSuiteRunHistoryPage(page)) return false;
  const run = page.items[0];
  const target = OFFLINE_TARGETS.find((option) => option.id === selection.targetId)!;
  return job.status === 'succeeded' && run.run_id === job.resource_id &&
    sameSuitePin(run.suite, { kind: 'suite', name: selection.suite.name, revision: selection.suite.revision, digest: selection.suite.digest }) &&
    run.dataset_name === selection.suite.dataset_name && run.dataset_revision === selection.suite.dataset_revision &&
    run.execution_mode === 'offline_mock' && run.target.name === target.name && run.target.revision === target.revision;
}

export function createSuiteRunClient(readCredential: CredentialSource) {
  const jobs = createLocalJobClient(readCredential, 'run');
  return {
    async submit(selection: SuiteRunSelection, key: string, writeCredential: CredentialSource, signal: AbortSignal): Promise<LocalJob> {
      if (runSelectionIssue(selection) || !/^run-[a-f0-9-]{36}$/.test(key)) throw failure(400, 'invalid_submission');
      const target = OFFLINE_TARGETS.find((option) => option.id === selection.targetId)!;
      const value = await jobs.submit({
        suite_name: selection.suite.name, suite_revision: selection.suite.revision,
        target_name: target.name, target_revision: target.revision,
      } satisfies components['schemas']['SuiteRunCreateRequest'], key, writeCredential, signal);
      if (!record(value) || !keys(value, ['schema_version', 'job', 'run']) ||
          value.schema_version !== 'run-submission/v2' || !isLocalJob(value.job, 'run')) throw failure(200);
      if (value.run != null && !matchesRun(value.run, selection, value.job)) throw failure(200);
      return value.job;
    },
    getJob: jobs.getJob,
  };
}
export type SuiteRunClient = ReturnType<typeof createSuiteRunClient>;
