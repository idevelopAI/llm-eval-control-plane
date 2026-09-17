import type {
  ReleaseDecision,
  ReleaseDecisionDistributions,
} from '../api/client';
import {
  releaseDecision,
  releaseCases,
  releaseDistributions,
} from './release-evidence';
import { suiteDecisionPage } from './suite-history';

// Synthetic evidence for a decision outside the newest-decision fixture page.
export const historicalDecisionPage = {
  ...suiteDecisionPage,
  items: [
    { ...suiteDecisionPage.items[0], created_at: '2026-08-26T12:20:00Z' },
  ],
};
const item = historicalDecisionPage.items[0];

export const historicalDecision: ReleaseDecision = {
  ...releaseDecision,
  decision_id: item.decision_id,
  decision_digest: item.decision_digest,
  baseline_run_id: item.baseline_run_id,
  candidate_run_id: item.candidate_run_id,
  created_at: item.created_at,
  suite: item.suite,
};

export const historicalCases = {
  ...releaseCases,
  decision_id: item.decision_id,
  next_cursor: null,
};

export const historicalDistributions: ReleaseDecisionDistributions = {
  ...releaseDistributions,
  decision_id: item.decision_id,
  baseline: { ...releaseDistributions.baseline, run_id: item.baseline_run_id },
  candidate: {
    ...releaseDistributions.candidate,
    run_id: item.candidate_run_id,
  },
};
