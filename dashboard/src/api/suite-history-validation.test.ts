import { describe, expect, it } from 'vitest';

import {
  suiteDecisionPage,
  suitePage,
  suitePin,
  suiteRunPage,
} from '../test/suite-history';
import {
  isSuiteDecisionHistoryPage,
  isSuitePage,
  isSuiteRunHistoryPage,
  sameSuitePin,
} from './suite-history-validation';

describe.each([
  [isSuitePage, suitePage],
  [isSuiteRunHistoryPage, suiteRunPage],
  [isSuiteDecisionHistoryPage, suiteDecisionPage],
] as const)('suite metadata contracts', (validate, fixture) => {
  it('accepts metadata and bounded empty pages', () => {
    expect(validate(fixture)).toBe(true);
    expect(validate({ ...fixture, items: [] })).toBe(true);
    expect(validate({ ...fixture, next_cursor: 'opaque_cursor-1' })).toBe(true);
  });

  it.each([
    { document: 'private-sentinel' },
    { next_cursor: 'x'.repeat(2049) },
    { next_cursor: 'unsafe cursor' },
    { schema_version: 'unknown/v1' },
    { items: [], next_cursor: 'nonterminal-empty' },
  ])('rejects unexpected or unbounded envelope fields %j', (change) => {
    expect(validate({ ...fixture, ...change })).toBe(false);
  });

  it('rejects duplicate identities, overlarge pages, and private item fields', () => {
    expect(
      validate({
        ...fixture,
        items: [fixture.items[0], fixture.items[0]],
      }),
    ).toBe(false);
    expect(
      validate({ ...fixture, items: Array(101).fill(fixture.items[0]) }),
    ).toBe(false);
    expect(
      validate({
        ...fixture,
        items: [{ ...fixture.items[0], output: 'private-sentinel' }],
      }),
    ).toBe(false);
  });

  it.each([
    'invalid',
    '2026-02-30T12:00:00Z',
    '2026-09-15T24:00:00Z',
    '2026-09-15T12:00:00',
  ])('rejects invalid UTC time %s', (created_at) => {
    expect(
      validate({
        ...fixture,
        items: [{ ...fixture.items[0], created_at }],
      }),
    ).toBe(false);
  });
});

describe('history identities', () => {
  it('binds name, revision, kind, and digest', () => {
    expect(sameSuitePin(suitePin, { ...suitePin })).toBe(true);
    for (const change of [
      { name: 'other' },
      { revision: 2 },
      { digest: null },
      { digest: `sha256:${'0'.repeat(64)}` },
      { kind: 'target' as const },
    ])
      expect(sameSuitePin(suitePin, { ...suitePin, ...change })).toBe(false);
  });

  it.each([
    { kind: 'target' },
    { digest: null },
    { digest: 'sha256:bad' },
    { name: 'invalid name' },
    { revision: 0 },
    { revision: 1.5 },
    { revision: Number.MAX_SAFE_INTEGER + 1 },
    { credential: 'private-sentinel' },
  ])('rejects invalid suite pins %j', (change) => {
    for (const [validate, fixture] of [
      [isSuiteRunHistoryPage, suiteRunPage],
      [isSuiteDecisionHistoryPage, suiteDecisionPage],
    ] as const) {
      expect(
        validate({
          ...fixture,
          items: [
            {
              ...fixture.items[0],
              suite: { ...suitePin, ...change },
            },
          ],
        }),
      ).toBe(false);
    }
  });

  it('rejects private or unresolved targets', () => {
    for (const change of [
      { digest: null },
      { kind: 'suite' },
      { input: 'private-sentinel' },
    ]) {
      expect(
        isSuiteRunHistoryPage({
          ...suiteRunPage,
          items: [
            {
              ...suiteRunPage.items[0],
              target: {
                ...suiteRunPage.items[0].target,
                ...change,
              },
            },
          ],
        }),
      ).toBe(false);
    }
  });

  it('rejects mixed suite history and chronological reversals', () => {
    const first = suiteRunPage.items[0];
    const older = {
      ...first,
      run_id: 'suite-run-001',
      created_at: '2026-09-15T12:09:00Z',
    };
    expect(
      isSuiteRunHistoryPage({ ...suiteRunPage, items: [first, older] }),
    ).toBe(true);
    expect(
      isSuiteRunHistoryPage({ ...suiteRunPage, items: [older, first] }),
    ).toBe(false);
    expect(
      isSuiteRunHistoryPage({
        ...suiteRunPage,
        items: [first, { ...older, suite: { ...suitePin, revision: 2 } }],
      }),
    ).toBe(false);
  });

  it('orders microseconds precisely and uses IDs only for real timestamp ties', () => {
    const first = {
      ...suiteRunPage.items[0],
      run_id: 'a',
      created_at: '2026-09-15T12:10:00.000002Z',
    };
    const older = {
      ...first,
      run_id: 'z',
      created_at: '2026-09-15T12:10:00.000001Z',
    };
    expect(
      isSuiteRunHistoryPage({ ...suiteRunPage, items: [first, older] }),
    ).toBe(true);
    expect(
      isSuiteRunHistoryPage({ ...suiteRunPage, items: [older, first] }),
    ).toBe(false);
    expect(
      isSuiteRunHistoryPage({
        ...suiteRunPage,
        items: [first, { ...older, created_at: first.created_at }],
      }),
    ).toBe(false);
  });
});
