'use client';

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ControlPlaneApiError } from '@/src/api/client';
import type { LocalJob } from '@/src/api/local-job-client';
import { OFFLINE_TARGETS, runSelectionIssue, type SuiteRunClient, type SuiteRunSelection } from '@/src/api/suite-run-client';
import { createRuntimeCredentialVault } from '@/src/security/runtime-credential-vault';
import styles from './suite-history-panel.module.css';

type Attempt = { selection: SuiteRunSelection; key: string };
type Props = {
  suite: SuiteRunSelection['suite'];
  projectId: string;
  client: SuiteRunClient;
  onAuthenticationFailure: () => void;
  onShowRuns: () => void;
};

/** Only the loopback development entry imports this run-submission surface. */
export function SuiteRunPanel({ suite, projectId, client, onAuthenticationFailure, onShowRuns }: Props) {
  const [opened, setOpened] = useState(false);
  const [targetId, setTargetId] = useState('');
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [job, setJob] = useState<LocalJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);
  const [credentialInput, setCredentialInput] = useState(0);
  const [writeVault] = useState(createRuntimeCredentialVault);
  const request = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const selection: SuiteRunSelection = { suite, targetId };
  const invalid = runSelectionIssue(selection);

  useEffect(() => () => {
    request.current?.abort();
    writeVault.clear();
  }, [writeVault]);

  function clear() {
    request.current?.abort();
    writeVault.clear();
    inFlight.current = false;
    setOpened(false);
    setTargetId('');
    setAttempt(null);
    setJob(null);
    setBusy(false);
    setIssue(null);
  }

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
      if (error instanceof ControlPlaneApiError && [401, 403].includes(error.status)) {
        clear();
        onAuthenticationFailure();
      } else {
        setIssue('The request could not be verified. An accepted run may still execute. Retry only the same submission, or refresh the known job; do not assume another run is needed.');
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
    const token = fields.get('runToken');
    fields.delete('runToken');
    form.reset();
    setCredentialInput((version) => version + 1);
    if (inFlight.current || job || invalid) return;
    try {
      if (typeof token !== 'string') throw new Error('Missing credential');
      writeVault.set({ projectId, accessToken: token });
      const next = attempt ?? { selection: structuredClone(selection), key: `run-${crypto.randomUUID()}` };
      setAttempt(next);
      void execute(async (signal) => {
        const accepted = await client.submit(next.selection, next.key, writeVault.credential, signal);
        if (!signal.aborted) setJob(accepted);
      });
    } catch {
      writeVault.clear();
      setIssue('Enter a valid local run write credential. It is cleared after each request.');
    }
  }

  function refreshJob() {
    if (!job) return;
    void execute(async (signal) => {
      const updated = await client.getJob(job, signal);
      if (!signal.aborted) setJob(updated);
    });
  }

  return <section className={styles.comparison} aria-labelledby="suite-run-heading">
    <div className={styles.heading}>
      <div>
        <h3 id="suite-run-heading">Run this suite</h3>
        <p className={styles.note}>Local offline evaluation · explicit submission · no model-provider calls.</p>
      </div>
      {!opened ? <button type="button" onClick={() => setOpened(true)}>Start an offline run</button> :
        <button type="button" disabled={busy} onClick={clear}>Close run form</button>}
    </div>
    {opened ? <>
      <div className={styles.protocol}>
        <strong>{suite.name} r{suite.revision}</strong>
        <code>{suite.digest}</code>
        <span>Project <code>{projectId}</code> · dataset {suite.dataset_name} r{suite.dataset_revision}</span>
        <span>{suite.gate_count} {suite.gate_count === 1 ? 'gate' : 'gates'} · {suite.evaluator_count} {suite.evaluator_count === 1 ? 'evaluator' : 'evaluators'} · offline_mock</span>
      </div>
      <label className={styles.selection}>
        Offline target
        <select value={targetId} disabled={busy || attempt != null} onChange={(event) => {
          setTargetId(event.target.value);
          setIssue(null);
        }}>
          <option value="">Choose a target</option>
          {OFFLINE_TARGETS.map((target) => <option key={target.id} value={target.id}>
            {target.name} r{target.revision}
          </option>)}
        </select>
      </label>
      <p className={styles.note}>Both targets use the same deterministic behavior and the suite&apos;s fixed policy. Different names do not represent different real models. No scenario or policy overrides are submitted.</p>
      {targetId && invalid ? <p role="alert" className={styles.error}>{invalid}</p> : null}
      {!job ? <form className={styles.writeForm} onSubmit={submit} autoComplete="off">
        <label htmlFor="run-write-token">Run write credential</label>
        <input key={credentialInput} id="run-write-token" name="runToken" type="password" required
          autoComplete="off" spellCheck={false} autoCapitalize="none" maxLength={47}
          disabled={busy || invalid != null} aria-describedby="run-credential-note" />
        <small id="run-credential-note">Use a same-project control-plane:write credential. The field clears immediately; its separate vault is discarded after the request. Your read session is unchanged.</small>
        <button type="submit" disabled={busy || invalid != null}>
          {busy ? 'Submitting run…' : attempt ? 'Retry same run' : 'Submit offline run'}
        </button>
      </form> : <div className={styles.protocol} aria-live="polite">
        <strong>Run job: {job.status.replaceAll('_', ' ')}</strong>
        <span>Job <code>{job.job_id}</code> · attempt {job.attempt_count}/{job.max_attempts}</span>
        <span>Run <code>{job.resource_id}</code></span>
        {['queued', 'running', 'cancel_requested'].includes(job.status) ?
          <button type="button" disabled={busy} onClick={refreshJob}>Refresh run status</button> : null}
        {job.status === 'succeeded' ? <>
          <p>Execution finished; case failures may still exist. Compare the completed run to evaluate release policy.</p>
          <button type="button" disabled={busy} onClick={onShowRuns}>Show completed runs</button>
        </> : null}
        {['failed', 'canceled'].includes(job.status) ? <p>No completed run is available. Inspect the job through the local API before submitting again.</p> : null}
      </div>}
      {attempt ? <p className={styles.note}>Submission key <code>{attempt.key}</code>. Retries keep this exact key and input. Closing, refreshing, or changing history discards this form, not an accepted job. Recover through the local job API or persisted history before starting another run.</p> : null}
      {busy ? <p role="status">Checking run…</p> : null}
      {issue ? <p role="alert" className={styles.error}>{issue}</p> : null}
    </> : null}
  </section>;
}
