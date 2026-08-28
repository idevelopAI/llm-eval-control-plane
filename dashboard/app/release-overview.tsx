'use client';

import { useMemo, useRef, useState } from 'react';

import DistributionComparison from './distribution-comparison';
import {
  demoDashboardModel,
  demoModelForGate,
} from '@/src/features/release-decisions/demo-release';
import type { ReleaseCaseChangeFilter } from '@/src/features/release-decisions/use-live-release';
import {
  filterOptions,
  type FilterId,
  type GateView,
  type ReleaseDashboardModel,
} from '@/src/features/release-decisions/view-model';

function formatScore(value: number | null | undefined) {
  return value == null ? '—' : value.toFixed(3);
}

function formatDelta(value: number | null | undefined) {
  if (value == null) return '—';
  if (value === 0) return '0.000';
  return `${value > 0 ? '+' : '−'}${Math.abs(value).toFixed(3)}`;
}

function gateScope(gate: GateView) {
  return gate.gate.slice ?? 'all cases';
}

function changeLabel(
  value: NonNullable<ReleaseDashboardModel['cases']>[number]['change'],
) {
  return value.replaceAll('_', ' ');
}

function passLabel(value: boolean | null) {
  if (value == null) return 'unavailable';
  return value ? 'passed' : 'failed';
}

function passClass(value: boolean | null) {
  if (value == null) return 'neutral-text';
  return value ? 'pass-text' : 'fail-text';
}

const caseChangeOptions: readonly Readonly<{
  id: ReleaseCaseChangeFilter;
  label: string;
}>[] = [
  { id: 'all', label: 'All transitions' },
  { id: 'newly_failing', label: 'Newly failing' },
  { id: 'newly_passing', label: 'Newly passing' },
  { id: 'unchanged_failing', label: 'Unchanged failing' },
  { id: 'unchanged_passing', label: 'Unchanged passing' },
  { id: 'incomparable', label: 'Incomparable' },
];

export type ReleaseOverviewViewProps = Readonly<{
  busy?: boolean;
  caseChangeFilter?: ReleaseCaseChangeFilter;
  caseDisplayLimitReached?: boolean;
  caseEvidenceIssue?: Readonly<{
    message: string;
    requestId: string | null;
  }> | null;
  caseLoadMoreAvailable?: boolean;
  distributionEvidenceIssue?: Readonly<{
    message: string;
    requestId: string | null;
  }> | null;
  model: ReleaseDashboardModel;
  onLoadMoreCases?: () => void;
  onRetryCaseEvidence?: () => void;
  onRetryDistributionEvidence?: () => void;
  onSelectCaseChange?: (change: ReleaseCaseChangeFilter) => void;
  onSelectGate: (gateId: string) => void;
  sourceLabel: string;
}>;

export function ReleaseOverviewView({
  busy = false,
  caseChangeFilter,
  caseDisplayLimitReached = false,
  caseEvidenceIssue = null,
  caseLoadMoreAvailable = false,
  distributionEvidenceIssue = null,
  model,
  onLoadMoreCases,
  onRetryCaseEvidence,
  onRetryDistributionEvidence,
  onSelectCaseChange,
  onSelectGate,
  sourceLabel,
}: ReleaseOverviewViewProps) {
  const [activeFilter, setActiveFilter] = useState<FilterId>('all');
  const [expandedCaseId, setExpandedCaseId] = useState<string | null>(null);
  const caseHeadingRef = useRef<HTMLHeadingElement>(null);

  const visibleGates = useMemo(
    () =>
      activeFilter === 'all'
        ? model.gates
        : model.gates.filter((gate) => gate.filter === activeFilter),
    [activeFilter, model.gates],
  );
  const visibleCases = useMemo(
    () =>
      model.cases?.filter((item) =>
        item.gateIds.some((gateId) => gateId === model.selectedGateId),
      ) ?? [],
    [model.cases, model.selectedGateId],
  );
  const selectedGate = model.gates.find(
    (gate) => gate.id === model.selectedGateId,
  );
  const failedGates = model.gates.filter((gate) => gate.gate.status === 'failed');
  const reviewGate = failedGates[0] ?? model.gates[0];
  const newlyFailingCases =
    model.cases?.filter((item) => item.change === 'newly_failing').length ??
    null;
  const releaseFailed = model.release.status === 'failed';

  function focusCaseInbox() {
    window.requestAnimationFrame(() => caseHeadingRef.current?.focus());
  }

  function selectFilter(filter: FilterId) {
    setActiveFilter(filter);
    setExpandedCaseId(null);
    const candidates =
      filter === 'all'
        ? model.gates
        : model.gates.filter((gate) => gate.filter === filter);
    const next = candidates.find((gate) => gate.gate.status === 'failed') ?? candidates[0];
    if (next && next.id !== model.selectedGateId) onSelectGate(next.id);
  }

  function selectGate(gate: GateView) {
    setActiveFilter(gate.filter);
    setExpandedCaseId(null);
    onSelectGate(gate.id);
    focusCaseInbox();
  }

  return (
    <div className="dashboard-shell" aria-busy={busy}>
      <header className="masthead">
        <a className="brand" href="#decision" aria-label="Eval Control home">
          <span className="brand-mark" aria-hidden="true">
            EC
          </span>
          <span>
            <strong>Eval Control</strong>
            <small>Release evidence</small>
          </span>
        </a>

        <div className="project-context" aria-label="Current project">
          <span>Project</span>
          <strong>{model.release.project}</strong>
        </div>

        <div className="mode-pill">
          <span className="mode-dot" aria-hidden="true" />
          <span>
            <strong>{model.release.executionMode}</strong>
            <small>
              {model.release.simulated
                ? 'Simulated latency and usage'
                : 'Observed latency and usage'}
            </small>
          </span>
        </div>
      </header>

      <main>
        <section
          className={`decision-hero ${releaseFailed ? 'is-failed' : 'is-passed'}`}
          id="decision"
        >
          <div className="hero-copy">
            <p className="eyebrow">
              Decision · {model.release.createdAt} · {model.release.spec}
            </p>
            <div className="verdict-line">
              <span className="verdict-icon" aria-hidden="true">
                {releaseFailed ? '×' : '✓'}
              </span>
              <div>
                <h1>{releaseFailed ? 'Release blocked' : 'Release passed'}</h1>
                <p>
                  {releaseFailed
                    ? `Blocked by ${failedGates.length} of ${model.gates.length} configured gates.`
                    : `All ${model.gates.length} configured gates passed the release policy.`}
                </p>
              </div>
            </div>
            <div className="comparison-identity" aria-label="Run comparison">
              <span>
                <small>Baseline</small>
                <strong>{model.release.baseline}</strong>
              </span>
              <span className="comparison-arrow" aria-hidden="true">
                →
              </span>
              <span>
                <small>Candidate</small>
                <strong>{model.release.candidate}</strong>
              </span>
              <span className="dataset-label">
                <small>Dataset</small>
                <strong>{model.release.dataset}</strong>
              </span>
            </div>
          </div>

          <div className="decision-summary">
            <div className="summary-number">
              <strong>{failedGates.length}</strong>
              <span>of {model.gates.length} gates failed</span>
            </div>
            <div className="summary-number secondary">
              <strong>{newlyFailingCases ?? '—'}</strong>
              <span>
                {newlyFailingCases == null
                  ? 'case projection unavailable'
                  : 'newly failing shown for selected gate'}
              </span>
            </div>
            {reviewGate ? (
              <button
                className="primary-action"
                type="button"
                onClick={() => selectGate(reviewGate)}
              >
                {releaseFailed ? 'Review failed gate' : 'Review gate evidence'}
                <span aria-hidden="true">→</span>
              </button>
            ) : null}
          </div>
        </section>

        <section className="filter-strip" aria-labelledby="filter-heading">
          <div>
            <p className="eyebrow" id="filter-heading">
              Slice lens
            </p>
            <div className="filter-list">
              {filterOptions.map((filter) => (
                <button
                  aria-pressed={activeFilter === filter.id}
                  className={activeFilter === filter.id ? 'is-active' : ''}
                  key={filter.id}
                  onClick={() => selectFilter(filter.id)}
                  type="button"
                >
                  {filter.label}
                </button>
              ))}
            </div>
          </div>
          <p className="delta-definition">
            Delta is always <strong>candidate − baseline</strong>
          </p>
        </section>

        <div className="decision-grid">
          <section className="ledger-panel" aria-labelledby="ledger-heading">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">Decision evidence</p>
                <h2 id="ledger-heading">Gate ledger</h2>
              </div>
              <span className="result-count">
                {visibleGates.length} shown · failed first
              </span>
            </div>

            <div className="gate-list">
              {visibleGates.length ? (
                visibleGates.map((item) => {
                  const { aggregate } = item.gate;
                  const isFailed = item.gate.status === 'failed';
                  const isSelected = item.id === model.selectedGateId;
                  return (
                    <button
                      aria-pressed={isSelected}
                      className={`gate-row ${isFailed ? 'is-failed' : 'is-passed'} ${isSelected ? 'is-selected' : ''}`}
                      key={item.id}
                      onClick={() => selectGate(item)}
                      type="button"
                    >
                      <span className="gate-status" aria-hidden="true">
                        {isFailed ? '×' : '✓'}
                      </span>
                      <span className="gate-identity">
                        <strong>{item.label}</strong>
                        <code>{item.gate.metric}</code>
                        <small>{gateScope(item)}</small>
                      </span>
                      <span className="score-comparison">
                        <span>
                          <small>Baseline</small>
                          <strong>{formatScore(aggregate.baseline.mean)}</strong>
                        </span>
                        <span className="score-arrow" aria-hidden="true">
                          →
                        </span>
                        <span>
                          <small>Candidate</small>
                          <strong>{formatScore(aggregate.candidate.mean)}</strong>
                        </span>
                      </span>
                      <span className="delta-cell">
                        <small>Delta</small>
                        <strong>{formatDelta(aggregate.delta)}</strong>
                      </span>
                      <span className="coverage-cell">
                        <small>Scored / attempted</small>
                        <strong>
                          {aggregate.baseline.scored}/{aggregate.baseline.attempted}
                          <span aria-hidden="true"> ↔ </span>
                          {aggregate.candidate.scored}/{aggregate.candidate.attempted}
                        </strong>
                        {aggregate.candidate.scored < 20 ? (
                          <em>Small sample · descriptive result</em>
                        ) : null}
                      </span>
                      <span className="gate-outcome">
                        <strong>{isFailed ? 'Failed' : 'Passed'}</strong>
                        {isFailed ? (
                          <span>
                            {item.gate.failure_codes.map((code) => (
                              <small key={code}>{code} failed</small>
                            ))}
                          </span>
                        ) : (
                          <small>Within release policy</small>
                        )}
                      </span>
                    </button>
                  );
                })
              ) : (
                <div className="empty-state">
                  No configured gate uses this slice family yet.
                </div>
              )}
            </div>
          </section>

          <aside className="case-panel" aria-labelledby="case-heading">
            <div className="panel-heading case-heading">
              <div>
                <p className="eyebrow">Regression inbox</p>
                <h2 id="case-heading" ref={caseHeadingRef} tabIndex={-1}>
                  Scoring evidence
                </h2>
              </div>
              <span className="case-count">
                {model.cases == null
                  ? 'Unavailable'
                  : `${visibleCases.length} shown · selected gate${model.casePageTruncated ? ' · more available' : ''}`}
              </span>
            </div>

            {selectedGate ? (
              <p className="scope-note">
                Scoped to <strong>{selectedGate.gate.metric}</strong> ·{' '}
                {gateScope(selectedGate)}
              </p>
            ) : (
              <p className="scope-note">Select a gate to inspect scoring evidence.</p>
            )}

            {caseChangeFilter && onSelectCaseChange ? (
              <div className="case-filter">
                <label htmlFor="case-change-filter">Case transition</label>
                <select
                  disabled={busy}
                  id="case-change-filter"
                  onChange={(event) =>
                    onSelectCaseChange(
                      event.currentTarget.value as ReleaseCaseChangeFilter,
                    )
                  }
                  value={caseChangeFilter}
                >
                  {caseChangeOptions.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}

            {model.cases == null ? (
              <div className="panel-evidence-error" role="alert">
                <strong>Case evidence is unavailable.</strong>
                <p>
                  {caseEvidenceIssue?.message ??
                    'The case projection could not be loaded.'}
                </p>
                {caseEvidenceIssue?.requestId ? (
                  <small>Request ID: {caseEvidenceIssue.requestId}</small>
                ) : null}
                {onRetryCaseEvidence ? (
                  <button
                    disabled={busy}
                    onClick={onRetryCaseEvidence}
                    type="button"
                  >
                    Retry case evidence
                  </button>
                ) : null}
              </div>
            ) : (
              <div className="case-list">
                {visibleCases.length ? (
                  visibleCases.map((item) => {
                    const expanded = expandedCaseId === item.id;
                    const evidenceId = `case-evidence-${encodeURIComponent(item.id)}`;
                    return (
                      <article className="case-card" key={item.id}>
                        <div className="case-title">
                          <div>
                            <span className="transition-badge">
                              {changeLabel(item.change)}
                            </span>
                            <h3>{item.id}</h3>
                          </div>
                          <span className="case-transition">
                            <span className={passClass(item.baselinePassed)}>
                              {passLabel(item.baselinePassed)}
                            </span>
                            <span aria-hidden="true">→</span>
                            <span className={passClass(item.candidatePassed)}>
                              {passLabel(item.candidatePassed)}
                            </span>
                          </span>
                        </div>

                        <code className="case-metric">{item.metric}</code>
                        <div className="slice-tags" aria-label="Case slices">
                          {item.slices.length ? (
                            item.slices.map((slice) => (
                              <span key={slice}>{slice}</span>
                            ))
                          ) : (
                            <span>no slice labels</span>
                          )}
                        </div>

                        <button
                          aria-controls={evidenceId}
                          aria-expanded={expanded}
                          aria-label={`${expanded ? 'Hide' : 'Inspect'} scoring evidence for ${item.id}`}
                          className="evidence-toggle"
                          onClick={() =>
                            setExpandedCaseId(expanded ? null : item.id)
                          }
                          type="button"
                        >
                          {expanded
                            ? 'Hide scoring evidence'
                            : 'Inspect scoring evidence'}
                          <span aria-hidden="true">{expanded ? '−' : '+'}</span>
                        </button>

                        {expanded ? (
                          <div className="case-evidence" id={evidenceId}>
                            <dl>
                              <div>
                                <dt>Baseline value</dt>
                                <dd>
                                  {formatScore(item.baselineValue)} ·{' '}
                                  {passLabel(item.baselinePassed)}
                                </dd>
                              </div>
                              <div>
                                <dt>Candidate value</dt>
                                <dd>
                                  {formatScore(item.candidateValue)} ·{' '}
                                  {passLabel(item.candidatePassed)}
                                </dd>
                              </div>
                              <div>
                                <dt>Case delta</dt>
                                <dd>{formatDelta(item.delta)}</dd>
                              </div>
                            </dl>
                            <p>
                              Score-only projection. Prompt, expected value,
                              target output, SQL, and stored rows are not present.
                            </p>
                          </div>
                        ) : null}
                      </article>
                    );
                  })
                ) : (
                  <div className="empty-state">
                    No cases are attached to this bounded gate projection.
                  </div>
                )}
              </div>
            )}

            {caseLoadMoreAvailable && onLoadMoreCases ? (
              <button
                className="load-more-cases"
                disabled={busy}
                onClick={onLoadMoreCases}
                type="button"
              >
                {busy ? 'Loading more cases…' : 'Load more cases'}
              </button>
            ) : null}
            {caseDisplayLimitReached ? (
              <p className="case-display-limit" role="status">
                This view reached its 500-case in-memory display limit.
              </p>
            ) : null}

            <div className="evidence-boundary">
              <span aria-hidden="true">i</span>
              <p>
                <strong>Safe evidence boundary</strong>
                Raw evaluation content is never transferred to this view.
              </p>
            </div>
          </aside>
        </div>

        {model.distributions ? (
          <DistributionComparison distributions={model.distributions} />
        ) : (
          <section
            className="distribution-panel panel-evidence-error"
            aria-labelledby="distribution-unavailable-heading"
            role="alert"
          >
            <p className="eyebrow">Privacy-bounded analytics</p>
            <h2 id="distribution-unavailable-heading">
              Distribution evidence is unavailable
            </h2>
            <p>
              {distributionEvidenceIssue?.message ??
                'The distribution projection could not be loaded.'}
            </p>
            {distributionEvidenceIssue?.requestId ? (
              <small>Request ID: {distributionEvidenceIssue.requestId}</small>
            ) : null}
            {onRetryDistributionEvidence ? (
              <button
                disabled={busy}
                onClick={onRetryDistributionEvidence}
                type="button"
              >
                Retry distribution evidence
              </button>
            ) : null}
          </section>
        )}

        <footer className="provenance-strip">
          <p>
            Decision <code>{model.release.decisionId}</code>
          </p>
          <p>
            Dataset digest <code>{model.release.datasetDigest}</code>
          </p>
          <p>
            Decision digest <code>{model.release.decisionDigest}</code>
          </p>
          <p>{sourceLabel}</p>
        </footer>
      </main>
    </div>
  );
}

export default function ReleaseOverview() {
  const [model, setModel] = useState(demoDashboardModel);
  return (
    <ReleaseOverviewView
      model={model}
      onSelectGate={(gateId) => setModel(demoModelForGate(gateId))}
      sourceLabel="Immutable fixture · no live request made"
    />
  );
}
