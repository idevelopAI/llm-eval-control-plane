'use client';

import { useMemo, useRef, useState } from 'react';

import {
  demoCases,
  demoGates,
  demoRelease,
  filterOptions,
  type FilterId,
  type GateFixture,
} from '@/src/features/release-decisions/demo-release';

function formatScore(value: number | null | undefined) {
  return value == null ? '—' : value.toFixed(3);
}

function formatDelta(value: number | null | undefined) {
  if (value == null) return '—';
  if (value === 0) return '0.000';
  return `${value > 0 ? '+' : '−'}${Math.abs(value).toFixed(3)}`;
}

function gateScope(gate: GateFixture) {
  return gate.gate.slice ?? 'all cases';
}

export default function ReleaseOverview() {
  const [activeFilter, setActiveFilter] = useState<FilterId>('all');
  const [expandedCaseId, setExpandedCaseId] = useState<string | null>(null);
  const [selectedGateId, setSelectedGateId] = useState<string | null>(
    'safety-refusal',
  );
  const caseHeadingRef = useRef<HTMLHeadingElement>(null);

  const visibleGates = useMemo(
    () =>
      activeFilter === 'all'
        ? demoGates
        : demoGates.filter((gate) => gate.filter === activeFilter),
    [activeFilter],
  );

  const visibleCases = useMemo(() => {
    if (selectedGateId) {
      return demoCases.filter((item) =>
        item.gateIds.some((gateId) => gateId === selectedGateId),
      );
    }
    if (activeFilter === 'all') return demoCases;
    const gateIds = demoGates
      .filter((gate) => gate.filter === activeFilter)
      .map((gate) => gate.id);
    return demoCases.filter((item) =>
      item.gateIds.some((gateId) =>
        gateIds.some((visibleGateId) => visibleGateId === gateId),
      ),
    );
  }, [activeFilter, selectedGateId]);

  function focusCaseInbox() {
    window.requestAnimationFrame(() => caseHeadingRef.current?.focus());
  }

  function selectFilter(filter: FilterId) {
    setActiveFilter(filter);
    setExpandedCaseId(null);
    setSelectedGateId(null);
  }

  function selectGate(gate: GateFixture) {
    setActiveFilter(gate.filter);
    setExpandedCaseId(null);
    setSelectedGateId(gate.id);
    focusCaseInbox();
  }

  const selectedGate = demoGates.find((gate) => gate.id === selectedGateId);

  return (
    <div className="dashboard-shell">
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
          <strong>{demoRelease.project}</strong>
        </div>

        <div className="mode-pill">
          <span className="mode-dot" aria-hidden="true" />
          <span>
            <strong>{demoRelease.executionMode}</strong>
            <small>Simulated latency and usage</small>
          </span>
        </div>
      </header>

      <main>
        <section className="decision-hero" id="decision">
          <div className="hero-copy">
            <p className="eyebrow">
              Decision · {demoRelease.createdAt} · {demoRelease.spec}
            </p>
            <div className="verdict-line">
              <span className="verdict-icon" aria-hidden="true">
                ×
              </span>
              <div>
                <h1>Release blocked</h1>
                <p>
                  The candidate crossed a safety threshold and exhausted the
                  allowed regression budget.
                </p>
              </div>
            </div>
            <div className="comparison-identity" aria-label="Run comparison">
              <span>
                <small>Baseline</small>
                <strong>{demoRelease.baseline}</strong>
              </span>
              <span className="comparison-arrow" aria-hidden="true">
                →
              </span>
              <span>
                <small>Candidate</small>
                <strong>{demoRelease.candidate}</strong>
              </span>
              <span className="dataset-label">
                <small>Dataset</small>
                <strong>{demoRelease.dataset}</strong>
              </span>
            </div>
          </div>

          <div className="decision-summary">
            <div className="summary-number">
              <strong>1</strong>
              <span>of 4 gates failed</span>
            </div>
            <div className="summary-number secondary">
              <strong>3</strong>
              <span>newly failing cases</span>
            </div>
            <button
              className="primary-action"
              type="button"
              onClick={() => selectGate(demoGates[0])}
            >
              Review safety regression
              <span aria-hidden="true">→</span>
            </button>
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
                  const isSelected = item.id === selectedGateId;
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
                {visibleCases.length}{' '}
                {visibleCases.length === 1 ? 'case' : 'cases'}
              </span>
            </div>

            {selectedGate ? (
              <p className="scope-note">
                Scoped to <strong>{selectedGate.gate.metric}</strong> ·{' '}
                {gateScope(selectedGate)}
              </p>
            ) : (
              <p className="scope-note">Showing the selected slice family.</p>
            )}

            <div className="case-list">
              {visibleCases.length ? (
                visibleCases.map((item) => {
                  const expanded = expandedCaseId === item.id;
                  return (
                    <article className="case-card" key={item.id}>
                      <div className="case-title">
                        <div>
                          <span className="transition-badge">
                            Newly failing
                          </span>
                          <h3>{item.id}</h3>
                        </div>
                        <span className="case-transition">
                          <span className="pass-text">passed</span>
                          <span aria-hidden="true">→</span>
                          <span className="fail-text">failed</span>
                        </span>
                      </div>

                      <code className="case-metric">{item.metric}</code>
                      <div className="slice-tags" aria-label="Case slices">
                        {item.slices.map((slice) => (
                          <span key={slice}>{slice}</span>
                        ))}
                      </div>

                      <button
                        aria-expanded={expanded}
                        className="evidence-toggle"
                        onClick={() =>
                          setExpandedCaseId(expanded ? null : item.id)
                        }
                        type="button"
                      >
                        {expanded ? 'Hide scoring evidence' : 'Inspect scoring evidence'}
                        <span aria-hidden="true">{expanded ? '−' : '+'}</span>
                      </button>

                      {expanded ? (
                        <div className="case-evidence">
                          <dl>
                            <div>
                              <dt>Baseline value</dt>
                              <dd>
                                {formatScore(item.baselineValue)} ·{' '}
                                {item.baselinePassed ? 'passed' : 'failed'}
                              </dd>
                            </div>
                            <div>
                              <dt>Candidate value</dt>
                              <dd>
                                {formatScore(item.candidateValue)} ·{' '}
                                {item.candidatePassed ? 'passed' : 'failed'}
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
                  No newly failing cases are attached to this gate projection.
                </div>
              )}
            </div>

            <div className="evidence-boundary">
              <span aria-hidden="true">i</span>
              <p>
                <strong>Safe evidence boundary</strong>
                Raw evaluation content is never transferred to this view.
              </p>
            </div>
          </aside>
        </div>

        <footer className="provenance-strip">
          <p>
            Decision <code>{demoRelease.decisionId}</code>
          </p>
          <p>
            Dataset digest <code>{demoRelease.datasetDigest}</code>
          </p>
          <p>
            Decision digest <code>{demoRelease.decisionDigest}</code>
          </p>
          <p>Immutable fixture · no live request made</p>
        </footer>
      </main>
    </div>
  );
}
