import type { components } from './generated/schema';

type SuitePage = components['schemas']['SuitePage'];
type RunPage = components['schemas']['SuiteRunHistoryPage'];
type DecisionPage = components['schemas']['SuiteDecisionHistoryPage'];
export type SuitePin = components['schemas']['ArtifactRef'];

const NAME = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const MODES = new Set([
  'offline_deterministic_fixture',
  'offline_mock',
  'live',
]);

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function keys(value: Record<string, unknown>, fields: readonly string[]) {
  return Object.keys(value).every((key) => fields.includes(key));
}

function matches(value: unknown, pattern: RegExp): value is string {
  return typeof value === 'string' && pattern.test(value);
}

function count(
  value: unknown,
  min: number,
  max = Number.MAX_SAFE_INTEGER,
): value is number {
  return (
    typeof value === 'number' &&
    Number.isSafeInteger(value) &&
    value >= min &&
    value <= max
  );
}

function timestamp(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length <= 40 &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value) &&
    Number.isFinite(Date.parse(value)) &&
    new Date(value).toISOString().slice(0, 19) === value.slice(0, 19)
  );
}

function timestampMicros(value: string): bigint {
  const fraction = /\.(\d+)Z$/.exec(value)?.[1] ?? '';
  return (
    BigInt(Math.floor(Date.parse(value) / 1000)) * BigInt(1000000) +
    BigInt(fraction.padEnd(6, '0'))
  );
}

function pin(value: unknown, kind: 'suite' | 'target'): value is SuitePin {
  return (
    record(value) &&
    keys(value, ['kind', 'name', 'revision', 'digest']) &&
    value.kind === kind &&
    matches(value.name, NAME) &&
    count(value.revision, 1) &&
    matches(value.digest, DIGEST)
  );
}

export function sameSuitePin(left: SuitePin, right: SuitePin): boolean {
  return (
    left.kind === 'suite' &&
    right.kind === 'suite' &&
    left.name === right.name &&
    left.revision === right.revision &&
    left.digest != null &&
    left.digest === right.digest
  );
}

function page(
  value: unknown,
  version: string,
  validateItem: (value: unknown) => boolean,
): value is Record<string, unknown> & { items: Record<string, unknown>[] } {
  return (
    record(value) &&
    keys(value, ['schema_version', 'items', 'next_cursor']) &&
    value.schema_version === version &&
    Array.isArray(value.items) &&
    value.items.length <= 100 &&
    value.items.every(validateItem) &&
    (value.next_cursor === undefined ||
      value.next_cursor === null ||
      matches(value.next_cursor, /^[A-Za-z0-9_-]{1,2048}$/)) &&
    (value.items.length > 0 || value.next_cursor == null)
  );
}

export function isSuitePage(value: unknown): value is SuitePage {
  if (
    !page(
      value,
      'suite-page/v1',
      (item) =>
        record(item) &&
        keys(item, [
          'schema_version',
          'name',
          'revision',
          'digest',
          'dataset_name',
          'dataset_revision',
          'evaluator_count',
          'metric_count',
          'slice_count',
          'gate_count',
          'execution_mode',
          'created_at',
        ]) &&
        item.schema_version === 'suite-list-item/v1' &&
        matches(item.name, NAME) &&
        count(item.revision, 1) &&
        matches(item.digest, DIGEST) &&
        matches(item.dataset_name, NAME) &&
        count(item.dataset_revision, 1) &&
        count(item.evaluator_count, 1, 32) &&
        count(item.metric_count, 1, 32) &&
        count(item.slice_count, 0, 128) &&
        count(item.gate_count, 1, 64) &&
        typeof item.execution_mode === 'string' &&
        MODES.has(item.execution_mode) &&
        timestamp(item.created_at),
    )
  )
    return false;
  return (
    new Set(value.items.map((item) => `${item.name}\0${item.revision}`))
      .size === value.items.length
  );
}

function newestFirst(
  items: Record<string, unknown>[],
  identity: string,
): boolean {
  if (new Set(items.map((item) => item[identity])).size !== items.length)
    return false;
  return items.every((item, index) => {
    if (index === 0) return true;
    const previous = items[index - 1];
    // PostgreSQL timestamps retain microseconds, beyond Date.parse precision.
    const time = timestampMicros(item.created_at as string);
    const previousTime = timestampMicros(previous.created_at as string);
    return (
      time < previousTime ||
      (time === previousTime &&
        (item[identity] as string) < (previous[identity] as string))
    );
  });
}

function singleSuite(items: Record<string, unknown>[]): boolean {
  return (
    items.length === 0 ||
    items.every((item) =>
      sameSuitePin(item.suite as SuitePin, items[0].suite as SuitePin),
    )
  );
}

export function isSuiteRunHistoryPage(value: unknown): value is RunPage {
  return (
    page(
      value,
      'suite-run-history-page/v1',
      (item) =>
        record(item) &&
        keys(item, [
          'schema_version',
          'run_id',
          'status',
          'execution_mode',
          'dataset_name',
          'dataset_revision',
          'result_digest',
          'created_at',
          'suite',
          'target',
        ]) &&
        item.schema_version === 'suite-run-history-item/v1' &&
        matches(item.run_id, RUN_ID) &&
        (item.status === 'completed' ||
          item.status === 'completed_with_failures') &&
        typeof item.execution_mode === 'string' &&
        MODES.has(item.execution_mode) &&
        matches(item.dataset_name, NAME) &&
        count(item.dataset_revision, 1) &&
        matches(item.result_digest, DIGEST) &&
        timestamp(item.created_at) &&
        pin(item.suite, 'suite') &&
        pin(item.target, 'target'),
    ) &&
    newestFirst(value.items, 'run_id') &&
    singleSuite(value.items)
  );
}

export function isSuiteDecisionHistoryPage(
  value: unknown,
): value is DecisionPage {
  return (
    page(
      value,
      'suite-decision-history-page/v1',
      (item) =>
        record(item) &&
        keys(item, [
          'schema_version',
          'decision_id',
          'status',
          'baseline_run_id',
          'candidate_run_id',
          'decision_digest',
          'created_at',
          'suite',
        ]) &&
        item.schema_version === 'suite-decision-history-item/v1' &&
        matches(item.decision_id, ID) &&
        (item.status === 'passed' || item.status === 'failed') &&
        matches(item.baseline_run_id, RUN_ID) &&
        matches(item.candidate_run_id, RUN_ID) &&
        matches(item.decision_digest, DIGEST) &&
        timestamp(item.created_at) &&
        pin(item.suite, 'suite'),
    ) &&
    newestFirst(value.items, 'decision_id') &&
    singleSuite(value.items)
  );
}
