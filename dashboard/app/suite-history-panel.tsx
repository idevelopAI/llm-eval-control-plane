'use client';

import { useEffect, useRef, useState } from 'react';
import {
  ControlPlaneApiError,
  type ControlPlaneClient,
  type SuitePage,
  type SuiteRunHistoryPage,
  type SuiteDecisionHistoryPage,
  type SuiteDecisionHistoryItem,
} from '@/src/api/client';
import {
  isSuitePage,
  isSuiteRunHistoryPage,
  isSuiteDecisionHistoryPage,
  sameSuitePin,
  type SuitePin,
} from '@/src/api/suite-history-validation';
import styles from './suite-history-panel.module.css';

type Suite = SuitePage['items'][number];
type History = {
  suite: Suite;
  runs: SuiteRunHistoryPage;
  decisions: SuiteDecisionHistoryPage;
};
const PAGE_SIZE = 20;
const DISPLAY_LIMIT = 100;
const suiteKey = (suite: Suite) => `${suite.name}@${suite.revision}`;
const suiteRef = (suite: Suite): SuitePin => ({
  kind: 'suite',
  name: suite.name,
  revision: suite.revision,
  digest: suite.digest,
});

function time(value: string) {
  return new Intl.DateTimeFormat('en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'UTC',
  }).format(new Date(value));
}

export function SuiteHistoryPanel({
  client,
  onAuthenticationFailure,
  onReviewDecision,
  selectedDecisionId = null,
  openingDecisionId = null,
}: {
  client: ControlPlaneClient;
  onAuthenticationFailure: () => void;
  onReviewDecision: (item: SuiteDecisionHistoryItem) => void;
  selectedDecisionId?: string | null;
  openingDecisionId?: string | null;
}) {
  const [opened, setOpened] = useState(false);
  const [catalog, setCatalog] = useState<SuitePage | null>(null);
  const [selected, setSelected] = useState<Suite | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [busy, setBusy] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);

  useEffect(
    () => () => {
      generation.current += 1;
      controller.current?.abort();
    },
    [],
  );

  async function execute(
    operation: (signal: AbortSignal) => Promise<() => void>,
  ) {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    const current = ++generation.current;
    setBusy(true);
    setIssue(null);
    try {
      const apply = await operation(request.signal);
      if (generation.current === current && !request.signal.aborted) apply();
    } catch (error) {
      if (generation.current !== current || request.signal.aborted) return;
      request.abort();
      if (
        error instanceof ControlPlaneApiError &&
        (error.status === 401 || error.status === 403)
      ) {
        setHistory(null);
        setCatalog(null);
        setSelected(null);
        onAuthenticationFailure();
      } else {
        setIssue(
          'Suite history could not be loaded. Retry with a fresh suite list.',
        );
      }
    } finally {
      if (generation.current === current) setBusy(false);
    }
  }

  async function readHistory(
    suite: Suite,
    signal: AbortSignal,
  ): Promise<History> {
    const query = {
      suite_name: suite.name,
      suite_revision: suite.revision,
      limit: PAGE_SIZE,
    };
    const [runs, decisions] = await Promise.all([
      client.listSuiteRuns(query, signal),
      client.listSuiteDecisions(query, signal),
    ]);
    const pin = suiteRef(suite);
    if (
      !runs.data.items.every((item) => sameSuitePin(item.suite, pin)) ||
      !decisions.data.items.every((item) => sameSuitePin(item.suite, pin))
    ) {
      throw new Error('Suite history identity mismatch');
    }
    return { suite, runs: runs.data, decisions: decisions.data };
  }

  function refresh() {
    setOpened(true);
    setCatalog(null);
    setHistory(null);
    setSelected(null);
    void execute(async (signal) => {
      const result = await client.listSuites({ limit: PAGE_SIZE }, signal);
      if (signal.aborted) return () => undefined;
      const suite = result.data.items[0] ?? null;
      const next = suite ? await readHistory(suite, signal) : null;
      return () => {
        setCatalog(result.data);
        setSelected(suite);
        setHistory(next);
      };
    });
  }

  function select(suite: Suite) {
    setSelected(suite);
    setHistory(null);
    void execute(async (signal) => {
      const next = await readHistory(suite, signal);
      return () => setHistory(next);
    });
  }

  function loadMore(kind: 'suites' | 'runs' | 'decisions') {
    const currentCatalog = catalog;
    const currentHistory = history;
    if (!currentCatalog || busy) return;
    void execute(async (signal) => {
      if (kind === 'suites') {
        const cursor = currentCatalog.next_cursor;
        if (!cursor || currentCatalog.items.length >= DISPLAY_LIMIT)
          return () => undefined;
        const result = await client.listSuites(
          {
            cursor,
            limit: Math.min(
              PAGE_SIZE,
              DISPLAY_LIMIT - currentCatalog.items.length,
            ),
          },
          signal,
        );
        const merged = {
          ...result.data,
          items: [...currentCatalog.items, ...result.data.items],
        };
        if (
          !isSuitePage(merged) ||
          result.data.next_cursor === cursor ||
          result.data.items.length === 0
        ) {
          throw new Error('Inconsistent suite pagination');
        }
        return () => setCatalog(merged);
      }
      if (!currentHistory) return () => undefined;
      const previous = currentHistory[kind];
      const cursor = previous.next_cursor;
      if (!cursor || previous.items.length >= DISPLAY_LIMIT)
        return () => undefined;
      const query = {
        suite_name: currentHistory.suite.name,
        suite_revision: currentHistory.suite.revision,
        cursor,
        limit: Math.min(PAGE_SIZE, DISPLAY_LIMIT - previous.items.length),
      };
      if (kind === 'runs') {
        const result = await client.listSuiteRuns(query, signal);
        const merged = {
          ...result.data,
          items: [...currentHistory.runs.items, ...result.data.items],
        };
        if (
          !isSuiteRunHistoryPage(merged) ||
          result.data.next_cursor === cursor ||
          result.data.items.length === 0
        ) {
          throw new Error('Inconsistent run pagination');
        }
        return () => setHistory({ ...currentHistory, runs: merged });
      }
      const result = await client.listSuiteDecisions(query, signal);
      const merged = {
        ...result.data,
        items: [...currentHistory.decisions.items, ...result.data.items],
      };
      if (
        !isSuiteDecisionHistoryPage(merged) ||
        result.data.next_cursor === cursor ||
        result.data.items.length === 0
      ) {
        throw new Error('Inconsistent decision pagination');
      }
      return () => setHistory({ ...currentHistory, decisions: merged });
    });
  }

  function pagination(
    kind: 'runs' | 'decisions',
    count: number,
    cursor: string | null | undefined,
  ) {
    return (
      <div className={styles.pagination}>
        <span>
          {count} {count === 1 ? kind.slice(0, -1) : kind} loaded · newest first
        </span>
        {cursor && count < DISPLAY_LIMIT ? (
          <button type="button" disabled={busy} onClick={() => loadMore(kind)}>
            Load older {kind}
          </button>
        ) : null}
        {cursor && count >= DISPLAY_LIMIT ? (
          <span>
            Display limit reached. Older records remain available through the
            API.
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <section className={styles.panel} aria-labelledby="suite-history-heading">
      <div className={styles.heading}>
        <div>
          <p className={styles.eyebrow}>Evaluation protocols</p>
          <h2 id="suite-history-heading">Suite history</h2>
        </div>
        <button type="button" disabled={busy} onClick={refresh}>
          {opened ? 'Refresh suites' : 'Browse suite history'}
        </button>
      </div>
      {!opened ? (
        <p>
          Inspect runs and release decisions pinned to an exact protocol
          revision.
        </p>
      ) : null}
      {busy ? <p role="status">Loading suite metadata…</p> : null}
      {issue ? (
        <p role="alert" className={styles.error}>
          {issue}
        </p>
      ) : null}
      {catalog?.items.length === 0 ? (
        <p>
          No evaluation suites registered yet. Register a suite through the
          local API to start its history.
        </p>
      ) : null}
      {catalog && catalog.items.length > 0 ? (
        <div className={styles.controls}>
          <label htmlFor="history-suite">Suite revision</label>
          <select
            id="history-suite"
            value={selected ? suiteKey(selected) : ''}
            onChange={(event) => {
              const suite = catalog.items.find(
                (item) => suiteKey(item) === event.target.value,
              );
              if (suite) select(suite);
            }}
          >
            {catalog.items.map((suite) => (
              <option key={suiteKey(suite)} value={suiteKey(suite)}>
                {suite.name} · revision {suite.revision}
              </option>
            ))}
          </select>
          {catalog.next_cursor && catalog.items.length < DISPLAY_LIMIT ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => loadMore('suites')}
            >
              Load more suites
            </button>
          ) : null}
          {catalog.next_cursor && catalog.items.length >= DISPLAY_LIMIT ? (
            <p>
              Showing 100 suite revisions. Further revisions remain available
              through the API.
            </p>
          ) : null}
        </div>
      ) : null}
      {history ? (
        <>
          <div className={styles.protocol}>
            <strong>
              {history.suite.name} · revision {history.suite.revision}
            </strong>
            <span>
              {history.suite.execution_mode === 'live'
                ? 'Live measurements'
                : 'Synthetic evaluation'}{' '}
              · {history.suite.gate_count} policy{' '}
              {history.suite.gate_count === 1 ? 'gate' : 'gates'}
            </span>
            <code>{history.suite.digest}</code>
          </div>
          <div className={styles.grid}>
            <section aria-labelledby="suite-runs-heading">
              <h3 id="suite-runs-heading">Evaluation runs</h3>
              {history.runs.items.length === 0 ? (
                <p>No completed runs for this suite revision.</p>
              ) : (
                <ul className={styles.records}>
                  {history.runs.items.map((item) => (
                    <li key={item.run_id}>
                      <div>
                        <strong>
                          {item.target.name} · r{item.target.revision}
                        </strong>
                        <span className={styles.badge}>
                          {item.status === 'completed'
                            ? 'Completed'
                            : 'Case failures'}
                        </span>
                      </div>
                      <code>{item.run_id}</code>
                      <span>{time(item.created_at)} UTC</span>
                    </li>
                  ))}
                </ul>
              )}
              {pagination(
                'runs',
                history.runs.items.length,
                history.runs.next_cursor,
              )}
            </section>
            <section aria-labelledby="suite-decisions-heading">
              <h3 id="suite-decisions-heading">Release decisions</h3>
              {history.decisions.items.length === 0 ? (
                <p>No release comparisons for this suite revision.</p>
              ) : (
                <ul className={styles.records}>
                  {history.decisions.items.map((item) => (
                    <li key={item.decision_id}>
                      <div>
                        <strong>{item.decision_id}</strong>
                        <span
                          className={`${styles.badge} ${item.status === 'failed' ? styles.blocked : styles.passed}`}
                        >
                          {item.status === 'failed' ? 'Blocked' : 'Passed'}
                        </span>
                      </div>
                      <span>
                        Baseline <code>{item.baseline_run_id}</code>
                      </span>
                      <span>
                        Candidate <code>{item.candidate_run_id}</code>
                      </span>
                      <span>{time(item.created_at)} UTC</span>
                      <button
                        type="button"
                        aria-label={`Review gates for ${item.decision_id}`}
                        aria-current={
                          selectedDecisionId === item.decision_id
                            ? 'true'
                            : undefined
                        }
                        disabled={
                          busy || openingDecisionId === item.decision_id
                        }
                        onClick={() => onReviewDecision(item)}
                      >
                        {openingDecisionId === item.decision_id
                          ? 'Opening decision…'
                          : selectedDecisionId === item.decision_id
                            ? 'Reviewing gates'
                            : 'Review gates'}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {pagination(
                'decisions',
                history.decisions.items.length,
                history.decisions.next_cursor,
              )}
            </section>
          </div>
          <p className={styles.note}>
            Read-only metadata. Queued and canceled jobs are outside this
            history; a blocked release is not a worker failure.
          </p>
        </>
      ) : null}
    </section>
  );
}
