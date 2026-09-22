import { describe, expect, it } from 'vitest';
import { suitePairPage, suiteTargetPage } from '../test/suite-history';
import {
  isSuiteTargetGroupPage,
  isSuiteTargetPairGroupPage,
  pairKey,
  sameTargetPin,
  targetKey,
} from './suite-history-validation';

describe.each([
  [isSuiteTargetGroupPage, suiteTargetPage],
  [isSuiteTargetPairGroupPage, suitePairPage],
] as const)('target group contracts', (validate, fixture) => {
  it('accepts bounded metadata pages and empty final pages', () => {
    expect(validate(fixture)).toBe(true);
    expect(validate({ ...fixture, items: [] })).toBe(true);
    expect(validate({ ...fixture, next_cursor: 'opaque_cursor-1' })).toBe(
      true,
    );
  });
  it.each([
    { document: 'private-sentinel' },
    { next_cursor: 'bad cursor' },
    { next_cursor: 'x'.repeat(2049) },
    { schema_version: 'unknown/v1' },
    { items: [], next_cursor: 'nonterminal-empty' },
  ])('rejects invalid envelopes %j', (change) => {
    expect(validate({ ...fixture, ...change })).toBe(false);
  });
  it('rejects duplicate, unbounded, private, and mixed-suite records', () => {
    const first = fixture.items[0];
    for (const items of [
      [first, first],
      Array(101).fill(first),
      [{ ...first, document: 'private-sentinel' }],
      [{ ...first, suite: { ...first.suite, digest: null } }],
      [first, { ...first, suite: { ...first.suite, revision: 2 } }],
    ])
      expect(validate({ ...fixture, items })).toBe(false);
  });
  it('rejects unresolved or malformed targets on either side', () => {
    const fields =
      fixture === suiteTargetPage
        ? ['target']
        : ['baseline_target', 'candidate_target'];
    for (const field of fields) {
      const item = fixture.items[0];
      const target = suiteTargetPage.items[0].target;
      for (const change of [
        { kind: 'suite' },
        { revision: true },
        { revision: 0 },
        { revision: 1.1 },
        { revision: Number.MAX_SAFE_INTEGER + 1 },
        { digest: null },
        { digest: 'sha256:bad' },
        { name: 'invalid name' },
        { input: 'private' },
      ])
        expect(
          validate({
            ...fixture,
            items: [{ ...item, [field]: { ...target, ...change } }],
          }),
        ).toBe(false);
    }
  });
});

describe('target identity ordering', () => {
  it('uses bytewise names, numeric revisions, then full digest', () => {
    const group = suiteTargetPage.items[0];
    const items = [
      { ...group, target: { ...group.target, name: 'A', revision: 1 } },
      {
        ...group,
        target: { ...group.target, name: 'A', revision: 10 },
      },
      { ...group, target: { ...group.target, name: 'a', revision: 1 } },
      {
        ...group,
        target: {
          ...group.target,
          name: 'a',
          revision: 1,
          digest: `sha256:${'d'.repeat(64)}`,
        },
      },
    ];
    expect(isSuiteTargetGroupPage({ ...suiteTargetPage, items })).toBe(true);
    expect(
      isSuiteTargetGroupPage({
        ...suiteTargetPage,
        items: [...items].reverse(),
      }),
    ).toBe(false);
    expect(targetKey(items[2].target)).not.toBe(targetKey(items[3].target));
    expect(sameTargetPin(items[2].target, items[3].target)).toBe(false);
    expect(sameTargetPin(group.target, { ...group.target })).toBe(true);
  });
  it('keeps forward/reverse pairs separate and sorts candidate ties by full identity', () => {
    const group = suitePairPage.items[0];
    const changed = {
      ...group,
      candidate_target: {
        ...group.candidate_target,
        digest: `sha256:${'d'.repeat(64)}`,
      },
    };
    const reverse = {
      ...group,
      baseline_target: group.candidate_target,
      candidate_target: group.baseline_target,
    };
    expect(
      isSuiteTargetPairGroupPage({
        ...suitePairPage,
        items: [group, changed, reverse],
      }),
    ).toBe(true);
    expect(
      isSuiteTargetPairGroupPage({
        ...suitePairPage,
        items: [changed, group],
      }),
    ).toBe(false);
    expect(pairKey(group)).not.toBe(pairKey(reverse));
    expect(pairKey(group)).not.toBe(pairKey(changed));
  });
});
