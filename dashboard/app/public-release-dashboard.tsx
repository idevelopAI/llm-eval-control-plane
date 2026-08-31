'use client';

import { useState } from 'react';

import { ReleaseOverviewView } from './release-overview';
import {
  demoDashboardModel,
  demoModelForGate,
} from '@/src/features/release-decisions/demo-release';

/**
 * Public Site entry point.
 *
 * This module intentionally has no API client, credential, storage, or live-mode
 * imports. The production build aliases the homepage directly to this component
 * so those capabilities cannot enter the hosted module graph.
 */
export default function PublicReleaseDashboard() {
  const [model, setModel] = useState(demoDashboardModel);

  return (
    <>
      <section className="source-control" aria-label="Dashboard data source">
        <div>
          <span className="source-badge fixture">example</span>
          <strong>Public example environment</strong>
        </div>
        <small>Synthetic data · no credentials, API, or model requests.</small>
      </section>
      <ReleaseOverviewView
        model={model}
        onSelectGate={(gateId) => setModel(demoModelForGate(gateId))}
        sourceLabel="Synthetic release evidence · zero API or model calls"
      />
    </>
  );
}
