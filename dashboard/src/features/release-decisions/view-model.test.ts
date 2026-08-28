import { describe, expect, it } from 'vitest';

import {
  releaseCases,
  releaseDecision,
  releaseDistributions,
} from '../../test/release-evidence';
import { buildReleaseDashboardModel } from './view-model';

describe('buildReleaseDashboardModel', () => {
  it('maps validated live evidence into a presentation-safe model', () => {
    const model = buildReleaseDashboardModel({
      cases: releaseCases,
      decision: releaseDecision,
      distributions: releaseDistributions,
      projectId: 'project-alpha',
    });

    expect(model.release).toMatchObject({
      baseline: 'models/baseline · revision 1',
      candidate: 'models/candidate · revision 2',
      executionMode: 'Offline mock',
      project: 'project-alpha',
      simulated: true,
      status: 'failed',
    });
    expect(model.gates[0]).toMatchObject({
      filter: 'language',
      id: '["quality.exact_match","language/de"]',
      label: 'Exact match · de',
    });
    expect(model.cases[0]).toMatchObject({
      change: 'newly_failing',
      gateIds: ['["quality.exact_match","language/de"]'],
      id: 'case-001',
    });
    expect(model.casePageTruncated).toBe(true);
  });

  it('rejects cross-decision, cross-gate, and cross-run evidence safely', () => {
    expect(() =>
      buildReleaseDashboardModel({
        cases: {
          ...releaseCases,
          decision_id: 'private-cross-decision-sentinel',
        },
        decision: releaseDecision,
        distributions: releaseDistributions,
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
    expect(() =>
      buildReleaseDashboardModel({
        cases: {
          ...releaseCases,
          items: [
            { ...releaseCases.items[0], gate_slice: 'safety/refusal' },
          ],
        },
        decision: releaseDecision,
        distributions: releaseDistributions,
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
    expect(() =>
      buildReleaseDashboardModel({
        cases: releaseCases,
        decision: releaseDecision,
        distributions: {
          ...releaseDistributions,
          candidate: {
            ...releaseDistributions.candidate,
            run_id: 'private-cross-run-sentinel',
          },
        },
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
  });

  it('rejects contradictory decision and gate evidence', () => {
    expect(() =>
      buildReleaseDashboardModel({
        cases: releaseCases,
        decision: { ...releaseDecision, status: 'passed' },
        distributions: releaseDistributions,
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
    expect(() =>
      buildReleaseDashboardModel({
        cases: releaseCases,
        decision: {
          ...releaseDecision,
          gates: [...releaseDecision.gates, releaseDecision.gates[0]],
        },
        distributions: releaseDistributions,
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
    expect(() =>
      buildReleaseDashboardModel({
        cases: releaseCases,
        decision: {
          ...releaseDecision,
          gates: [
            {
              ...releaseDecision.gates[0],
              aggregate: {
                ...releaseDecision.gates[0].aggregate,
                evaluator: {
                  ...releaseDecision.gates[0].aggregate.evaluator,
                  name: 'builtin/unpinned-sentinel',
                },
              },
            },
          ],
        },
        distributions: releaseDistributions,
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
  });

  it('rejects mathematically inconsistent case and distribution evidence', () => {
    expect(() =>
      buildReleaseDashboardModel({
        cases: {
          ...releaseCases,
          items: [{ ...releaseCases.items[0], delta: 0.25 }],
        },
        decision: releaseDecision,
        distributions: releaseDistributions,
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
    expect(() =>
      buildReleaseDashboardModel({
        cases: releaseCases,
        decision: releaseDecision,
        distributions: {
          ...releaseDistributions,
          score: {
            ...releaseDistributions.score,
            candidate: {
              ...releaseDistributions.score.candidate,
              statistics: {
                ...releaseDistributions.score.candidate.statistics,
                mean: 0.5,
              },
            },
          },
        },
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
    expect(() =>
      buildReleaseDashboardModel({
        cases: {
          ...releaseCases,
          items: [releaseCases.items[0], { ...releaseCases.items[0], case_id: 'case-002' }],
        },
        decision: releaseDecision,
        distributions: releaseDistributions,
        projectId: 'project-alpha',
      }),
    ).toThrow('Release dashboard evidence is inconsistent.');
  });
});
