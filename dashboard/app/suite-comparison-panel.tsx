'use client';

import { useEffect, useRef, useState, type FormEvent } from 'react';
import {
  ControlPlaneApiError,
  type ControlPlaneClient,
  type SuiteDecisionHistoryItem,
  type SuiteTargetPairGroupPage,
} from '@/src/api/client';
import {
  comparisonIssue,
  matchesComparisonDecision,
  type ComparisonJob,
  type ComparisonSelection,
  type ComparisonSuite,
  type SuiteComparisonClient,
  type SuiteRun,
} from '@/src/api/suite-comparison-client';
import { createRuntimeCredentialVault } from '@/src/security/runtime-credential-vault';
import styles from './suite-history-panel.module.css';

type Attempt = { selection: ComparisonSelection; key: string };
type Props = {
  suite: ComparisonSuite;
  runs: SuiteRun[];
  projectId: string;
  client: ControlPlaneClient;
  comparisons: SuiteComparisonClient;
  onAuthenticationFailure: () => void;
  onReviewDecision: (item: SuiteDecisionHistoryItem, pair?: SuiteTargetPairGroupPage['items'][number]) => void;
};

/** This component is only imported by the loopback-only live entry. */
export function SuiteComparisonPanel({
  suite,
  runs,
  projectId,
  client,
  comparisons,
  onAuthenticationFailure,
  onReviewDecision,
}: Props) {
  const [opened, setOpened] = useState(false);
  const [baselineId, setBaselineId] = useState('');
  const [candidateId, setCandidateId] = useState('');
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [job, setJob] = useState<ComparisonJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);
  const [credentialInput, setCredentialInput] = useState(0);
  const request = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const [writeVault] = useState(createRuntimeCredentialVault);
  const baseline = runs.find((run) => run.run_id === baselineId);
  const candidate = runs.find((run) => run.run_id === candidateId);
  const selection = baseline && candidate ? { suite, baseline, candidate } : null;
  const incompatibility = selection ? comparisonIssue(selection) : null;

  useEffect(() => () => {
    request.current?.abort();
    writeVault.clear();
  }, [writeVault]);

  async function execute(operation: (signal: AbortSignal) => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setIssue(null);
    try {
      await operation(controller.signal);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ControlPlaneApiError && (error.status === 401 || error.status === 403)) {
        controller.abort();
        writeVault.clear();
        setAttempt(null);
        setJob(null);
        setBaselineId('');
        setCandidateId('');
        setOpened(false);
        setBusy(false);
        onAuthenticationFailure();
      } else {
        setIssue('The request could not be verified. A submitted job may still run. Retry only this same submission, or refresh a known job; do not assume a new comparison is needed.');
      }
    } finally {
      writeVault.clear();
      inFlight.current = false;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const accessToken = fields.get('comparisonToken');
    fields.delete('comparisonToken');
    form.reset();
    // Replace the uncontrolled password element as well as resetting the form.
    // A retry gets a fresh input rather than reusing its tracked value.
    setCredentialInput((version) => version + 1);
    if (inFlight.current || job || (!attempt && (!selection || incompatibility))) return;
    try {
      if (typeof accessToken !== 'string') throw new Error('Missing credential');
      writeVault.set({ projectId, accessToken });
      const next = attempt ?? { selection: structuredClone(selection!), key: `comparison-${crypto.randomUUID()}` };
      setAttempt(next);
      void execute(async (signal) => {
        const accepted = await comparisons.submit(next.selection, next.key, writeVault.credential, signal);
        if (!signal.aborted) setJob(accepted);
      });
    } catch {
      writeVault.clear();
      setIssue('Enter a valid local comparison write credential. It is cleared after each request.');
    }
  }

  function refreshJob() {
    if (!job) return;
    void execute(async (signal) => {
      const next = await comparisons.getJob(job, signal);
      if (!signal.aborted) setJob(next);
    });
  }

  function review() {
    if (!attempt || !job || job.status !== 'succeeded') return;
    void execute(async (signal) => {
      const { data } = await client.getReleaseDecision(job.resource_id, signal);
      if (signal.aborted) return;
      if (!matchesComparisonDecision(data, attempt.selection, job)) throw new Error('Decision mismatch');
      onReviewDecision({
        schema_version: 'suite-decision-history-item/v1',
        decision_id: data.decision_id, status: data.status,
        baseline_run_id: data.baseline_run_id, candidate_run_id: data.candidate_run_id,
        decision_digest: data.decision_digest, created_at: data.created_at, suite: data.suite!,
      }, {
        schema_version: 'suite-target-pair-group/v1', suite: data.suite!,
        baseline_target: attempt.selection.baseline.target, candidate_target: attempt.selection.candidate.target,
      });
    });
  }

  function close() {
    request.current?.abort();
    writeVault.clear();
    inFlight.current = false;
    setOpened(false);
    setAttempt(null);
    setJob(null);
    setBusy(false);
    setIssue(null);
    setBaselineId('');
    setCandidateId('');
  }

  return (
    <section className={styles.comparison} aria-labelledby="create-comparison-heading">
      <div className={styles.heading}>
        <div>
          <h3 id="create-comparison-heading">Compare existing runs</h3>
          <p className={styles.note}>Local only · choose the direction explicitly · no model calls or new evaluation runs.</p>
        </div>
        {!opened ? <button type="button" onClick={() => setOpened(true)}>Choose runs to compare</button> :
          <button type="button" disabled={busy} onClick={close}>Close comparison</button>}
      </div>
      {opened ? <>
        <p>Choose from the runs loaded in this history. Use All targets and load older runs before selecting. Refreshing, paging, or changing history clears this form; accepted jobs are not canceled.</p>
        <div className={styles.grid}>
          {(['Baseline', 'Candidate'] as const).map((role) => <label className={styles.selection} key={role}>
            {role} run
            <select aria-label={`${role} run`} value={role === 'Baseline' ? baselineId : candidateId}
              disabled={busy || attempt != null} onChange={(event) => {
                if (role === 'Baseline') setBaselineId(event.target.value); else setCandidateId(event.target.value);
                setIssue(null);
              }}>
              <option value="">Choose a {role.toLowerCase()} run</option>
              {runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id} · {run.target.name} r{run.target.revision}</option>)}
            </select>
          </label>)}
        </div>
        {incompatibility ? <p role="alert" className={styles.error}>{incompatibility}</p> : null}
        {selection && !incompatibility ? <div className={styles.protocol}>
          <strong>Comparison direction: baseline → candidate</strong>
          <span>Project <code>{projectId}</code> · suite {suite.name} r{suite.revision}</span>
          <code>{suite.digest}</code>
          {([['Baseline', selection.baseline], ['Candidate', selection.candidate]] as const).map(([role, item]) => {
            return <div className={styles.pin} key={role}>
              <strong>{role}: {item.run_id}</strong>
              <span>{item.target.name} r{item.target.revision} · {item.status}</span>
              <span>Target <code>{item.target.digest}</code></span>
              <span>Result <code>{item.result_digest}</code></span>
            </div>;
          })}
          <small>Metadata preflight checks suite, dataset revision, and execution mode. The server revalidates immutable evidence and applies the pinned policy. Runs with case failures remain comparable; policy coverage decides the release outcome.</small>
        </div> : null}
        {!job ? <form className={styles.writeForm} onSubmit={submit} autoComplete="off">
          <label htmlFor="comparison-write-token">Comparison write credential</label>
          <input key={credentialInput} id="comparison-write-token" name="comparisonToken" type="password" required autoComplete="off"
            spellCheck={false} autoCapitalize="none" maxLength={47} disabled={busy || !selection || incompatibility != null}
            aria-describedby="comparison-credential-note" />
          <small id="comparison-credential-note">Use a credential with control-plane:write for this project. Your read-only session is not upgraded. The field clears immediately and the credential is discarded after this request.</small>
          <button type="submit" disabled={busy || !selection || incompatibility != null}>
            {busy ? 'Submitting comparison…' : attempt ? 'Retry same comparison' : 'Submit comparison'}
          </button>
        </form> : <div className={styles.protocol} aria-live="polite">
          <strong>Comparison job: {job.status.replaceAll('_', ' ')}</strong>
          <span>Job <code>{job.job_id}</code></span>
          <span>Decision <code>{job.resource_id}</code> · attempt {job.attempt_count}/{job.max_attempts}</span>
          <p>A succeeded job may produce a blocked release. Job success means the policy was evaluated.</p>
          {['queued', 'running', 'cancel_requested'].includes(job.status) ?
            <button type="button" disabled={busy} onClick={refreshJob}>Refresh comparison status</button> : null}
          {job.status === 'succeeded' ? <button type="button" disabled={busy} onClick={review}>Review comparison gates</button> : null}
          {job.status === 'failed' || job.status === 'canceled' ? <p>No successful decision is available. Inspect the job through the local API before starting another comparison.</p> : null}
        </div>}
        {attempt ? <p className={styles.note}>Submission key <code>{attempt.key}</code>. Retries keep the exact same key and inputs. Closing this form or changing history does not cancel an accepted job; its durable evidence remains in history.</p> : null}
        {busy ? <p role="status">Checking comparison…</p> : null}
        {issue ? <p role="alert" className={styles.error}>{issue}</p> : null}
      </> : null}
    </section>
  );
}
