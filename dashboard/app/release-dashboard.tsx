'use client';

import { useCallback, useState, useSyncExternalStore, type FormEvent } from 'react';

import { ReleaseOverviewView } from './release-overview';
import {
  createControlPlaneClient,
  type ReleaseDecisionPage,
} from '@/src/api/client';
import {
  demoDashboardModel,
  demoModelForGate,
} from '@/src/features/release-decisions/demo-release';
import {
  LIVE_CASE_DISPLAY_LIMIT,
  useLiveRelease,
} from '@/src/features/release-decisions/use-live-release';
import { isLoopbackDashboardLocation } from '@/src/security/dashboard-origin';
import { createRuntimeCredentialVault } from '@/src/security/runtime-credential-vault';

function subscribeToLocation() {
  return () => undefined;
}

function browserLoopbackSnapshot() {
  return isLoopbackDashboardLocation(globalThis.location);
}

function serverLoopbackSnapshot() {
  return false;
}

function ConnectionPanel({
  error,
  onSubmit,
}: {
  error: string | null;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <section className="connection-panel" aria-labelledby="connection-heading">
      <p className="eyebrow">Local read-only session</p>
      <h1 id="connection-heading">Connect to live release evidence</h1>
      <p>
        The credential is held in this tab&apos;s volatile memory and sent only to
        the same-origin loopback API proxy. It is never stored in browser storage,
        cookies, URLs, or logs.
      </p>
      {error ? <p role="alert" className="connection-error">{error}</p> : null}
      <form onSubmit={onSubmit}>
        <label htmlFor="project-id">Project ID</label>
        <input
          autoComplete="off"
          id="project-id"
          maxLength={128}
          name="projectId"
          pattern="[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
          required
          spellCheck={false}
        />
        <label htmlFor="access-token">Read-only access token</label>
        <input
          autoComplete="off"
          id="access-token"
          maxLength={47}
          name="accessToken"
          pattern="cpk_[A-Za-z0-9_-]{43}"
          required
          spellCheck={false}
          type="password"
        />
        <button className="connect-action" type="submit">
          Connect and load newest decision
        </button>
      </form>
    </section>
  );
}

function StatePanel({
  children,
  role = 'status',
}: {
  children: React.ReactNode;
  role?: 'alert' | 'status';
}) {
  return (
    <section
      className="remote-state"
      role={role}
      aria-live={role === 'alert' ? 'assertive' : 'polite'}
    >
      {children}
    </section>
  );
}

function decisionTimestamp(value: string) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) return 'time unavailable';
  return new Intl.DateTimeFormat('en-GB', {
    day: '2-digit',
    hour: '2-digit',
    hour12: false,
    minute: '2-digit',
    month: 'short',
    timeZone: 'UTC',
  }).format(timestamp);
}

function DecisionPicker({
  busy,
  decisions,
  onSelect,
  selectedDecisionId,
}: {
  busy: boolean;
  decisions: ReleaseDecisionPage;
  onSelect: (decisionId: string) => void;
  selectedDecisionId: string;
}) {
  return (
    <section className="decision-picker" aria-label="Live decision history">
      <label htmlFor="live-decision">Decision history</label>
      <select
        aria-busy={busy}
        disabled={busy}
        id="live-decision"
        onChange={(event) => onSelect(event.currentTarget.value)}
        value={selectedDecisionId}
      >
        {decisions.items.map((item) => (
          <option key={item.decision_id} value={item.decision_id}>
            {item.status === 'failed' ? 'Blocked' : 'Passed'} ·{' '}
            {decisionTimestamp(item.created_at)} UTC · {item.decision_id}
          </option>
        ))}
      </select>
      <small>
        {decisions.items.length} newest immutable decision
        {decisions.items.length === 1 ? '' : 's'} loaded
        {decisions.next_cursor ? ' · older decisions available through the API' : ''}
      </small>
    </section>
  );
}

export default function ReleaseDashboard() {
  const [sourceMode, setSourceMode] = useState<'fixture' | 'live'>('fixture');
  const [fixtureModel, setFixtureModel] = useState(demoDashboardModel);
  const [credentialError, setCredentialError] = useState<string | null>(null);
  const [liveProject, setLiveProject] = useState<string | null>(null);
  const loopbackEnabled = useSyncExternalStore(
    subscribeToLocation,
    browserLoopbackSnapshot,
    serverLoopbackSnapshot,
  );
  const [vault] = useState(() => createRuntimeCredentialVault());
  const [client] = useState(() => createControlPlaneClient(vault.credential));
  const clearCredential = useCallback(() => {
    vault.clear();
    setLiveProject(null);
  }, [vault]);
  const live = useLiveRelease({
    client,
    onAuthenticationFailure: clearCredential,
  });

  function enterLiveMode() {
    if (!loopbackEnabled) return;
    setCredentialError(null);
    live.disconnect();
    setSourceMode('live');
  }

  function returnToFixture() {
    live.disconnect();
    vault.clear();
    setCredentialError(null);
    setLiveProject(null);
    setSourceMode('fixture');
  }

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const projectId = fields.get('projectId');
    const accessToken = fields.get('accessToken');
    form.reset();
    if (typeof projectId !== 'string' || typeof accessToken !== 'string') {
      setCredentialError('The control-plane credential format is invalid.');
      return;
    }
    try {
      vault.set({ accessToken, projectId });
    } catch {
      setCredentialError('The control-plane credential format is invalid.');
      return;
    }
    setCredentialError(null);
    setLiveProject(projectId);
    void live.connect(projectId);
  }

  const sourceControl = (
    <section className="source-control" aria-label="Dashboard data source">
      <div>
        <span className={`source-badge ${sourceMode}`}>{sourceMode}</span>
        <strong>
          {sourceMode === 'fixture'
            ? 'Immutable portfolio fixture'
            : 'Local live control plane'}
        </strong>
      </div>
      {sourceMode === 'fixture' ? (
        <button disabled={!loopbackEnabled} onClick={enterLiveMode} type="button">
          Use local live data
        </button>
      ) : (
        <button onClick={returnToFixture} type="button">
          Disconnect and return to fixture
        </button>
      )}
      {!loopbackEnabled && sourceMode === 'fixture' ? (
        <small>
          Browser bearer entry is disabled outside an HTTP loopback origin.
        </small>
      ) : null}
    </section>
  );

  if (sourceMode === 'fixture') {
    return (
      <>
        {sourceControl}
        <ReleaseOverviewView
          model={fixtureModel}
          onSelectGate={(gateId) => setFixtureModel(demoModelForGate(gateId))}
          sourceLabel="Immutable fixture · no live request made"
        />
      </>
    );
  }

  if (live.state.kind === 'disconnected') {
    return (
      <>
        {sourceControl}
        <ConnectionPanel error={credentialError} onSubmit={connect} />
      </>
    );
  }

  if (live.state.kind === 'loading' && !live.state.previous) {
    return (
      <>
        {sourceControl}
        <StatePanel>
          <p className="eyebrow">Live API</p>
          <h1>Loading bounded release evidence</h1>
          <p>
            {live.state.stage === 'list'
              ? 'Finding the newest immutable release decision…'
              : 'Loading redacted cases and aggregate distributions…'}
          </p>
        </StatePanel>
      </>
    );
  }

  if (live.state.kind === 'empty') {
    const emptyProjectId = live.state.projectId;
    return (
      <>
        {sourceControl}
        <StatePanel>
          <p className="eyebrow">Live API</p>
          <h1>No release decisions yet</h1>
          <p>This project returned an empty, valid decision collection.</p>
          <button onClick={() => void live.connect(emptyProjectId)} type="button">
            Refresh live data
          </button>
        </StatePanel>
      </>
    );
  }

  if (live.state.kind === 'error' && !live.state.previous) {
    const canRetry = liveProject != null && vault.hasCredential();
    return (
      <>
        {sourceControl}
        <StatePanel role="alert">
          <p className="eyebrow">Live API error</p>
          <h1>Live evidence is unavailable</h1>
          <p>{live.state.message}</p>
          {live.state.requestId ? <p>Request ID: {live.state.requestId}</p> : null}
          {canRetry ? (
            <button onClick={() => void live.connect(liveProject)} type="button">
              Retry live request
            </button>
          ) : (
            <ConnectionPanel error={credentialError} onSubmit={connect} />
          )}
        </StatePanel>
      </>
    );
  }

  const ready =
    live.state.kind === 'ready' ? live.state.value : live.state.previous;
  if (!ready) return null;

  return (
    <>
      {sourceControl}
      {live.state.kind === 'error' ? (
        <div className="inline-live-error" role="alert">
          <strong>Selected gate evidence could not be refreshed.</strong>{' '}
          {live.state.message}
          {live.state.requestId ? ` Request ID: ${live.state.requestId}` : ''}
        </div>
      ) : null}
      <DecisionPicker
        busy={live.state.kind === 'loading'}
        decisions={ready.decisions}
        onSelect={(decisionId) => void live.selectDecision(decisionId)}
        selectedDecisionId={ready.decision.decision_id}
      />
      <ReleaseOverviewView
        busy={live.state.kind === 'loading'}
        caseChangeFilter={ready.caseChange}
        caseDisplayLimitReached={
          ready.casePage.next_cursor != null &&
          ready.casePage.items.length >= LIVE_CASE_DISPLAY_LIMIT
        }
        caseLoadMoreAvailable={
          ready.casePage.next_cursor != null &&
          ready.casePage.items.length < LIVE_CASE_DISPLAY_LIMIT
        }
        model={ready.model}
        onLoadMoreCases={() => void live.loadMoreCases()}
        onSelectCaseChange={(caseChange) =>
          void live.selectCaseChange(caseChange)
        }
        onSelectGate={live.selectGate}
        sourceLabel="Live API · redacted response contracts · credential held in memory"
      />
    </>
  );
}
