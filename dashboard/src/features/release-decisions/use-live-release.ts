'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  ControlPlaneApiError,
  type ControlPlaneClient,
  type ReleaseDecision,
  type ReleaseDecisionPage,
} from '../../api/client';
import {
  buildReleaseDashboardModel,
  gateId,
  type ReleaseDashboardModel,
} from './view-model';

type ReadyRelease = Readonly<{
  decision: ReleaseDecision;
  decisions: ReleaseDecisionPage;
  model: ReleaseDashboardModel;
  projectId: string;
}>;

export type LiveReleaseState =
  | Readonly<{ kind: 'disconnected' }>
  | Readonly<{ kind: 'loading'; previous?: ReadyRelease; stage: 'list' | 'evidence' }>
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
      previous,
      projectId,
    }: {
      decision: ReleaseDecision;
      decisions: ReleaseDecisionPage;
      gateMetric: string;
      gateSlice: string | null;
      previous?: ReadyRelease;
      projectId: string;
    }) => {
      const { controller, generation } = beginRequest();
      setState({ kind: 'loading', previous, stage: 'evidence' });
      const scopedQuery = gateSlice == null ? {} : { gate_slice: gateSlice };
      try {
        const [caseResult, distributionResult] = await Promise.all([
          client.listReleaseDecisionCases(
            decision.decision_id,
            { limit: 100, metric: gateMetric, ...scopedQuery },
            controller.signal,
          ),
          client.getReleaseDecisionDistributions(
            decision.decision_id,
            { metric: gateMetric, ...scopedQuery },
            controller.signal,
          ),
        ]);
        if (!currentRequest(generation, controller)) return;
        const value = {
          decision,
          decisions,
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
        previous,
        projectId: previous.projectId,
      });
    },
    [loadEvidence],
  );

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

  return { connect, disconnect, selectGate, state };
}
