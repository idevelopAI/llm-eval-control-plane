import type {
  SuiteTargetGroupPage,
  SuiteTargetPairGroupPage,
} from '@/src/api/client';
import {
  pairKey,
  targetKey,
  type SuitePin,
} from '@/src/api/suite-history-validation';
import styles from './suite-history-panel.module.css';

export type HistoryFilters = {
  target: SuitePin | null;
  pair: SuiteTargetPairGroupPage['items'][number] | null;
};
export const ALL_TARGETS: HistoryFilters = { target: null, pair: null };

export function runTargetFilter({ target }: HistoryFilters) {
  return target
    ? {
        target_name: target.name,
        target_revision: target.revision,
        target_digest: target.digest,
      }
    : {};
}

export function decisionTargetFilter({ pair }: HistoryFilters) {
  return pair
    ? {
        baseline_target_name: pair.baseline_target.name,
        baseline_target_revision: pair.baseline_target.revision,
        baseline_target_digest: pair.baseline_target.digest,
        candidate_target_name: pair.candidate_target.name,
        candidate_target_revision: pair.candidate_target.revision,
        candidate_target_digest: pair.candidate_target.digest,
      }
    : {};
}

function label(target: SuitePin, options: SuitePin[]) {
  const digest = target.digest!.slice(7);
  let length = 12;
  while (
    length < digest.length &&
    options.some(
      (other) =>
        other.name === target.name &&
        other.revision === target.revision &&
        other.digest !== target.digest &&
        other.digest!.slice(7, 7 + length) === digest.slice(0, length),
    )
  ) {
    length += 1;
  }
  return `${target.name} · r${target.revision} · ${digest.slice(0, length)}`;
}

function Pin({ target, label: prefix }: { target: SuitePin; label: string }) {
  return (
    <p className={styles.pin}>
      <span>
        {prefix}: {target.name} · revision {target.revision}
      </span>
      <code>{target.digest}</code>
    </p>
  );
}

export function TargetHistoryFilters({
  targets,
  pairs,
  filters,
  busy,
  onChange,
  onLoadMore,
}: {
  targets: SuiteTargetGroupPage;
  pairs: SuiteTargetPairGroupPage;
  filters: HistoryFilters;
  busy: boolean;
  onChange: (filters: HistoryFilters) => void;
  onLoadMore: (kind: 'targets' | 'pairs') => void;
}) {
  const pairTargets = pairs.items.flatMap((pair) => [
    pair.baseline_target,
    pair.candidate_target,
  ]);
  function more(
    kind: 'targets' | 'pairs',
    page: SuiteTargetGroupPage | SuiteTargetPairGroupPage,
  ) {
    return (
      <div className={styles.pagination}>
        <span>
          {page.items.length}{' '}
          {page.items.length === 1 ? kind.slice(0, -1) : kind} loaded ·
          identity order
        </span>
        {page.next_cursor && page.items.length < 100 ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onLoadMore(kind)}
          >
            Load more {kind === 'targets' ? 'targets' : 'pairs'}
          </button>
        ) : null}
        {page.next_cursor && page.items.length >= 100 ? (
          <span>
            Showing 100 groups. Further groups remain available through the
            API.
          </span>
        ) : null}
      </div>
    );
  }
  return (
    <div className={`${styles.grid} ${styles.filters}`}>
      <div>
        <label htmlFor="history-target">Run target</label>
        <select
          id="history-target"
          value={filters.target ? targetKey(filters.target) : ''}
          onChange={(event) => {
            const target = targets.items.find(
              (item) => targetKey(item.target) === event.target.value,
            )?.target;
            if (event.target.value === '' || target)
              onChange({ ...filters, target: target ?? null });
          }}
        >
          <option value="">All targets</option>
          {targets.items.map(({ target }) => (
            <option key={targetKey(target)} value={targetKey(target)}>
              {label(
                target,
                targets.items.map((item) => item.target),
              )}
            </option>
          ))}
        </select>
        {filters.target ? (
          <Pin target={filters.target} label="Target" />
        ) : null}
        {targets.items.length === 0 ? (
          <p>No target groups recorded for this suite.</p>
        ) : null}
        {more('targets', targets)}
      </div>
      <div>
        <label htmlFor="history-pair">Decision target pair</label>
        <select
          id="history-pair"
          value={filters.pair ? pairKey(filters.pair) : ''}
          onChange={(event) => {
            const pair = pairs.items.find(
              (item) => pairKey(item) === event.target.value,
            );
            if (event.target.value === '' || pair)
              onChange({ ...filters, pair: pair ?? null });
          }}
        >
          <option value="">All baseline → candidate pairs</option>
          {pairs.items.map((pair) => (
            <option key={pairKey(pair)} value={pairKey(pair)}>
              {label(pair.baseline_target, pairTargets)} →{' '}
              {label(pair.candidate_target, pairTargets)}
            </option>
          ))}
        </select>
        {filters.pair ? (
          <>
            <Pin target={filters.pair.baseline_target} label="Baseline" />
            <Pin target={filters.pair.candidate_target} label="Candidate" />
          </>
        ) : null}
        {pairs.items.length === 0 ? (
          <p>No baseline/candidate pairs recorded for this suite.</p>
        ) : null}
        {more('pairs', pairs)}
      </div>
      <p className={styles.filterNote}>
        Each filter applies to its own column. Options show shortened digests;
        selected identities are shown in full.
      </p>
    </div>
  );
}
