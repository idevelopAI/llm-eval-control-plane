'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  ControlPlaneApiError,
  type ControlPlaneClient,
  type ReleaseCaseChange,
  type ReleaseDecision,
  type ReleaseDecisionCasePage,
  type ReleaseDecisionDistributions,
  type ReleaseDecisionPage,
} from '../../api/client';
import {
  buildReleaseDashboardModel,
  gateId,
  type ReleaseDashboardModel,
} from './view-model';

export type ReleaseCaseChangeFilter = 'all' | ReleaseCaseChange;
export const LIVE_CASE_DISPLAY_LIMIT = 500;

type ReadyRelease = Readonly<{
  caseChange: ReleaseCaseChangeFilter;
  casePage: ReleaseDecisionCasePage;
  decision: ReleaseDecision;
  decisions: ReleaseDecisionPage;
  distributions: ReleaseDecisionDistributions;
  model: ReleaseDashboardModel;
  projectId: string;
}>;

export type LiveReleaseState =
  | Readonly<{ kind: 'disconnected' }>
  | Readonly<{
      kind: 'loading';
      previous?: ReadyRelease;
      stage: 'cases' | 'decision' | 'evidence' | 'list';
    }>
  | Readonly<{ decisions: ReleaseDecisionPage; kind: 'empty'; projectId: string }>
  | Readonly<{ kind: 'ready'; value: ReadyRelease }>
  | Readonly<{
      kind: 'error';
      message: string;
      previous?: ReadyRelease;
      requestId: string | null;
    }>;

function abortError(error: unknown): boolean {
  return (
    typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'
  );
}

function safeError(error: unknown): { message: string; requestId: string | null } {
  if (error instanceof ControlPlaneApiError) {
    return { message: error.message, requestId: error.requestId };
  }
  return {
    message: 'Live release evidence could not be loaded.',
    requestId: null,
  };
}

function listItemMatchesDecision(
  item: ReleaseDecisionPage['items'][number],
  decision: ReleaseDecision,
): boolean {
  return (
    item.decision_id === decision.decision_id &&
    item.status === decision.status &&
    item.baseline_run_id === decision.baseline_run_id &&
    item.candidate_run_id === decision.candidate_run_id &&
    item.created_at === decision.created_at &&
    item.decision_digest === decision.decision_digest
  );
}

function decisionPageIsConsistent(page: ReleaseDecisionPage): boolean {
  const ids = new Set(page.items.map((item) => item.decision_id));
  if (ids.size !== page.items.length) return false;
  return page.items.every((item, index) => {
    const timestamp = Date.parse(item.created_at);
    if (!Number.isFinite(timestamp)) return false;
    if (index === 0) return true;
    return timestamp <= Date.parse(page.items[index - 1].created_at);
  });
}

function casePageMatchesFilter(
  page: ReleaseDecisionCasePage,
  change: ReleaseCaseChangeFilter,
): boolean {
  return (
    new Set(page.items.map((item) => item.case_id)).size === page.items.length &&
    (change === 'all' || page.items.every((item) => item.change === change))
  );
}

export function useLiveRelease({
  client,
  onAuthenticationFailure,
}: {
  client: ControlPlaneClient;
  onAuthenticationFailure: () => void;
}) {
  const [state, setState] = useState<LiveReleaseState>({ kind: 'disconnected' });
  const controllerRef = useRef<AbortController | null>(null);
  const generationRef = useRef(0);
  const readyRef = useRef<ReadyRelease | null>(null);

  const beginRequest = useCallback(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    generationRef.current += 1;
    return { controller, generation: generationRef.current };
  }, []);

  const currentRequest = useCallback(
    (generation: number, controller: AbortController) =>
      generationRef.current === generation && !controller.signal.aborted,
    [],
  );

  const loadEvidence = useCallback(
    async ({
      decision,
      decisions,
      gateMetric,
      gateSlice,
      caseChange,
      previous,
      projectId,
    }: {
      decision: ReleaseDecision;
      decisions: ReleaseDecisionPage;
      gateMetric: string;
      gateSlice: string | null;
      caseChange: ReleaseCaseChangeFilter;
      previous?: ReadyRelease;
      projectId: string;
    }) => {
      const { controller, generation } = beginRequest();
      setState({ kind: 'loading', previous, stage: 'evidence' });
      const scopedQuery = gateSlice == null ? {} : { gate_slice: gateSlice };
      const changeQuery = caseChange === 'all' ? {} : { change: caseChange };
      try {
        const [caseResult, distributionResult] = await Promise.all([
          client.listReleaseDecisionCases(
            decision.decision_id,
            {
              limit: 100,
              metric: gateMetric,
              ...scopedQuery,
              ...changeQuery,
            },
            controller.signal,
          ),
          client.getReleaseDecisionDistributions(
            decision.decision_id,
            { metric: gateMetric, ...scopedQuery },
            controller.signal,
          ),
        ]);
        if (!currentRequest(generation, controller)) return;
        if (!casePageMatchesFilter(caseResult.data, caseChange)) {
          throw new Error('Release case projection is inconsistent');
        }
        const value = {
          caseChange,
          casePage: caseResult.data,
          decision,
          decisions,
          distributions: distributionResult.data,
          model: buildReleaseDashboardModel({
            cases: caseResult.data,
            decision,
            distributions: distributionResult.data,
            projectId,
          }),
          projectId,
        } satisfies ReadyRelease;
        readyRef.current = value;
        setState({ kind: 'ready', value });
      } catch (error) {
        if (!currentRequest(generation, controller) || abortError(error)) return;
        controller.abort();
        const authenticationFailure =
          error instanceof ControlPlaneApiError &&
          (error.status === 401 || error.status === 403);
        if (authenticationFailure) {
          readyRef.current = null;
          onAuthenticationFailure();
        }
        setState({
          kind: 'error',
          previous: authenticationFailure ? undefined : previous,
          ...safeError(error),
        });
      }
    },
    [beginRequest, client, currentRequest, onAuthenticationFailure],
  );

  const connect = useCallback(
    async (projectId: string) => {
      readyRef.current = null;
      const { controller, generation } = beginRequest();
      setState({ kind: 'loading', stage: 'list' });
      try {
        const decisions = await client.listReleaseDecisions(
          { limit: 20, order: 'desc' },
          controller.signal,
        );
        if (!currentRequest(generation, controller)) return;
        if (!decisionPageIsConsistent(decisions.data)) {
          throw new Error('Release decision list is inconsistent');
        }
        if (decisions.data.items.length === 0) {
          setState({ decisions: decisions.data, kind: 'empty', projectId });
          return;
        }
        const newest = decisions.data.items[0];
        const detail = await client.getReleaseDecision(
          newest.decision_id,
          controller.signal,
        );
        if (!currentRequest(generation, controller)) return;
        if (!listItemMatchesDecision(newest, detail.data)) {
          throw new Error('Release summary identity mismatch');
        }
        const gate =
          detail.data.gates.find((item) => item.status === 'failed') ??
          detail.data.gates[0];
        if (!gate) throw new Error('Release decision has no gates');
        await loadEvidence({
          decision: detail.data,
          decisions: decisions.data,
          gateMetric: gate.metric,
          gateSlice: gate.slice ?? null,
          caseChange: 'all',
          projectId,
        });
      } catch (error) {
        if (!currentRequest(generation, controller) || abortError(error)) return;
        if (
          error instanceof ControlPlaneApiError &&
          (error.status === 401 || error.status === 403)
        ) {
          onAuthenticationFailure();
        }
        setState({ kind: 'error', ...safeError(error) });
      }
    },
    [beginRequest, client, currentRequest, loadEvidence, onAuthenticationFailure],
  );

  const selectDecision = useCallback(
    async (decisionId: string) => {
      const previous = readyRef.current;
      if (!previous || previous.decision.decision_id === decisionId) return;
      const item = previous.decisions.items.find(
        (candidate) => candidate.decision_id === decisionId,
      );
      if (!item) {
        setState({
          kind: 'error',
          message: 'The selected release decision is unavailable.',
          previous,
          requestId: null,
        });
        return;
      }

      const { controller, generation } = beginRequest();
      setState({ kind: 'loading', previous, stage: 'decision' });
      try {
        const detail = await client.getReleaseDecision(decisionId, controller.signal);
        if (!currentRequest(generation, controller)) return;
        if (!listItemMatchesDecision(item, detail.data)) {
          throw new Error('Release summary identity mismatch');
        }
        const gate =
          detail.data.gates.find((candidate) => candidate.status === 'failed') ??
          detail.data.gates[0];
        if (!gate) throw new Error('Release decision has no gates');
        await loadEvidence({
          decision: detail.data,
          decisions: previous.decisions,
          gateMetric: gate.metric,
          gateSlice: gate.slice ?? null,
          caseChange: 'all',
          previous,
          projectId: previous.projectId,
        });
      } catch (error) {
        if (!currentRequest(generation, controller) || abortError(error)) return;
        controller.abort();
        const authenticationFailure =
          error instanceof ControlPlaneApiError &&
          (error.status === 401 || error.status === 403);
        if (authenticationFailure) {
          readyRef.current = null;
          onAuthenticationFailure();
        }
        setState({
          kind: 'error',
          previous: authenticationFailure ? undefined : previous,
          ...safeError(error),
        });
      }
    },
    [
      beginRequest,
      client,
      currentRequest,
      loadEvidence,
      onAuthenticationFailure,
    ],
  );

  const selectGate = useCallback(
    (selectedGateId: string) => {
      const previous = readyRef.current;
      if (!previous) return;
      const gate = previous.decision.gates.find(
        (item) => gateId(item.metric, item.slice) === selectedGateId,
      );
      if (!gate) {
        setState({
          kind: 'error',
          message: 'The selected release gate is unavailable.',
          previous,
          requestId: null,
        });
        return;
      }
      void loadEvidence({
        decision: previous.decision,
        decisions: previous.decisions,
        gateMetric: gate.metric,
        gateSlice: gate.slice ?? null,
        caseChange: previous.caseChange,
        previous,
        projectId: previous.projectId,
      });
    },
    [loadEvidence],
  );

  const selectCaseChange = useCallback(
    async (caseChange: ReleaseCaseChangeFilter) => {
      const previous = readyRef.current;
      if (!previous || previous.caseChange === caseChange) return;
      const metric = previous.model.distributions.score.metric;
      const gateSlice = previous.model.distributions.score.gate_slice ?? null;
      const scopedQuery = gateSlice == null ? {} : { gate_slice: gateSlice };
      const changeQuery = caseChange === 'all' ? {} : { change: caseChange };
      const { controller, generation } = beginRequest();
      setState({ kind: 'loading', previous, stage: 'evidence' });
      try {
        const cases = await client.listReleaseDecisionCases(
          previous.decision.decision_id,
          { limit: 100, metric, ...scopedQuery, ...changeQuery },
          controller.signal,
        );
        if (!currentRequest(generation, controller)) return;
        if (!casePageMatchesFilter(cases.data, caseChange)) {
          throw new Error('Release case projection is inconsistent');
        }
        const value = {
          ...previous,
          caseChange,
          casePage: cases.data,
          model: buildReleaseDashboardModel({
            cases: cases.data,
            decision: previous.decision,
            distributions: previous.distributions,
            projectId: previous.projectId,
          }),
        } satisfies ReadyRelease;
        readyRef.current = value;
        setState({ kind: 'ready', value });
      } catch (error) {
        if (!currentRequest(generation, controller) || abortError(error)) return;
        controller.abort();
        const authenticationFailure =
          error instanceof ControlPlaneApiError &&
          (error.status === 401 || error.status === 403);
        if (authenticationFailure) {
          readyRef.current = null;
          onAuthenticationFailure();
        }
        setState({
          kind: 'error',
          previous: authenticationFailure ? undefined : previous,
          ...safeError(error),
        });
      }
    },
    [beginRequest, client, currentRequest, onAuthenticationFailure],
  );

  const loadMoreCases = useCallback(async () => {
    const previous = readyRef.current;
    const cursor = previous?.casePage.next_cursor;
    if (
      !previous ||
      !cursor ||
      previous.casePage.items.length >= LIVE_CASE_DISPLAY_LIMIT
    ) {
      return;
    }
    const metric = previous.distributions.score.metric;
    const gateSlice = previous.distributions.score.gate_slice ?? null;
    const scopedQuery = gateSlice == null ? {} : { gate_slice: gateSlice };
    const changeQuery =
      previous.caseChange === 'all' ? {} : { change: previous.caseChange };
    const limit = Math.min(
      100,
      LIVE_CASE_DISPLAY_LIMIT - previous.casePage.items.length,
    );
    const { controller, generation } = beginRequest();
    setState({ kind: 'loading', previous, stage: 'cases' });
    try {
      const cases = await client.listReleaseDecisionCases(
        previous.decision.decision_id,
        { cursor, limit, metric, ...scopedQuery, ...changeQuery },
        controller.signal,
      );
      if (!currentRequest(generation, controller)) return;
      const existingIds = new Set(
        previous.casePage.items.map((item) => item.case_id),
      );
      if (
        !casePageMatchesFilter(cases.data, previous.caseChange) ||
        cases.data.items.some((item) => existingIds.has(item.case_id)) ||
        (cases.data.next_cursor != null && cases.data.next_cursor === cursor)
      ) {
        throw new Error('Release case pagination is inconsistent');
      }
      const casePage = {
        ...cases.data,
        items: [...previous.casePage.items, ...cases.data.items],
      } satisfies ReleaseDecisionCasePage;
      const value = {
        ...previous,
        casePage,
        model: buildReleaseDashboardModel({
          cases: casePage,
          decision: previous.decision,
          distributions: previous.distributions,
          projectId: previous.projectId,
        }),
      } satisfies ReadyRelease;
      readyRef.current = value;
      setState({ kind: 'ready', value });
    } catch (error) {
      if (!currentRequest(generation, controller) || abortError(error)) return;
      controller.abort();
      const authenticationFailure =
        error instanceof ControlPlaneApiError &&
        (error.status === 401 || error.status === 403);
      if (authenticationFailure) {
        readyRef.current = null;
        onAuthenticationFailure();
      }
      setState({
        kind: 'error',
        previous: authenticationFailure ? undefined : previous,
        ...safeError(error),
      });
    }
  }, [beginRequest, client, currentRequest, onAuthenticationFailure]);

  const disconnect = useCallback(() => {
    generationRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    readyRef.current = null;
    setState({ kind: 'disconnected' });
  }, []);

  useEffect(
    () => () => {
      generationRef.current += 1;
      controllerRef.current?.abort();
    },
    [],
  );

  return {
    connect,
    disconnect,
    loadMoreCases,
    selectCaseChange,
    selectDecision,
    selectGate,
    state,
  };
}
