import { createHash } from 'node:crypto';

import { describe, expect, it } from 'vitest';

import { publicSyntheticFixture } from './demo-release';
import {
  assertPublicFixtureSafe,
  PUBLIC_FIXTURE_CASE_ID_PREFIX,
  PUBLIC_FIXTURE_DECISION_ID_PREFIX,
  PUBLIC_FIXTURE_RUN_ID_PREFIX,
  serializePublicFixture,
} from './public-fixture-safety';

type MutableRecord = Record<string, unknown>;

function mutableFixture(): MutableRecord {
  return structuredClone(publicSyntheticFixture) as unknown as MutableRecord;
}

function record(value: unknown, label: string): MutableRecord {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Test fixture path is not an object: ${label}`);
  }
  return value as MutableRecord;
}

function array(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`Test fixture path is not an array: ${label}`);
  }
  return value;
}

function releaseOf(fixture: MutableRecord): MutableRecord {
  return record(fixture.release, 'release');
}

function casesOf(fixture: MutableRecord): MutableRecord[] {
  return array(fixture.cases, 'cases').map((item, index) =>
    record(item, `cases[${index}]`),
  );
}

function distributionsOf(fixture: MutableRecord): MutableRecord[] {
  return array(fixture.distributions, 'distributions').map((item, index) =>
    record(item, `distributions[${index}]`),
  );
}

function gatesOf(fixture: MutableRecord): MutableRecord[] {
  return array(fixture.gates, 'gates').map((item, index) =>
    record(item, `gates[${index}]`),
  );
}

function reverseKeysRecursively(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(reverseKeysRecursively);
  if (value === null || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value)
      .reverse()
      .map(([key, child]) => [key, reverseKeysRecursively(child)]),
  );
}

describe('public synthetic fixture safety contract', () => {
  it('accepts the checked payload and pins its canonical SHA-256 digest', () => {
    expect(() => assertPublicFixtureSafe(publicSyntheticFixture)).not.toThrow();

    const canonical = serializePublicFixture(publicSyntheticFixture);
    const reordered = reverseKeysRecursively(publicSyntheticFixture);
    const digest = createHash('sha256').update(canonical, 'utf8').digest('hex');

    expect(serializePublicFixture(reordered)).toBe(canonical);
    expect(digest).toBe(
      'a4715938ded251ee184574cd4d9484e43ff478fb347b6ed6afa1f715b9641730',
    );
  });

  it('uses unmistakably synthetic namespaces for every public identifier', () => {
    expect(publicSyntheticFixture.release.decisionId).toMatch(
      new RegExp(`^${PUBLIC_FIXTURE_DECISION_ID_PREFIX}`),
    );
    for (const item of publicSyntheticFixture.cases) {
      expect(item.id).toMatch(new RegExp(`^${PUBLIC_FIXTURE_CASE_ID_PREFIX}`));
    }
    for (const distribution of publicSyntheticFixture.distributions) {
      expect(distribution.baseline.run_id).toMatch(
        new RegExp(`^${PUBLIC_FIXTURE_RUN_ID_PREFIX}`),
      );
      expect(distribution.candidate.run_id).toMatch(
        new RegExp(`^${PUBLIC_FIXTURE_RUN_ID_PREFIX}`),
      );
    }
  });

  it.each([
    'prompt',
    'raw_output',
    'sql',
    'rows',
    'credentials',
    'email_address',
  ])('rejects recursively nested denied key %s', (deniedKey) => {
    const fixture = mutableFixture();
    releaseOf(fixture).review = {
      safeEnvelope: [{ [deniedKey]: 'synthetic-redacted-sentinel' }],
    };

    expect(() => assertPublicFixtureSafe(fixture)).toThrow(/denied key/);
  });

  it('matches denied keys exactly instead of rejecting safe aggregate names', () => {
    const fixture = mutableFixture();
    releaseOf(fixture).aggregateSafetyProof = {
      credential_status: 'not-present',
      output_units: 7,
      pii_policy: 'synthetic-only',
      prompt_count: 0,
      rows_scored: 40,
      sql_metric: 'not-collected',
    };

    expect(() => assertPublicFixtureSafe(fixture)).not.toThrow();
  });

  it.each([
    ['email address', `synthetic.person${'@'}private.example`],
    ['URL', `https${'://'}private.example/evidence`],
    ['credential', ['sk', 'syntheticsecretmaterial000000'].join('-')],
    ['credential', `Bearer ${'synthetic-token-material'}`],
  ])('rejects a suspicious %s string without exposing it', (kind, value) => {
    const fixture = mutableFixture();
    releaseOf(fixture).note = value;

    let message = '';
    try {
      assertPublicFixtureSafe(fixture);
    } catch (error) {
      message = error instanceof Error ? error.message : String(error);
    }

    expect(message).toContain(kind);
    expect(message).not.toContain(value);
  });

  it.each([
    ['decision', (fixture: MutableRecord) => {
      releaseOf(fixture).decisionId = 'decision-regression-001';
    }],
    ['case', (fixture: MutableRecord) => {
      casesOf(fixture)[0].id = 'case-refusal-de-001';
    }],
    ['run', (fixture: MutableRecord) => {
      const baseline = record(distributionsOf(fixture)[0].baseline, 'baseline');
      baseline.run_id = 'run-baseline-001';
    }],
  ])('rejects an unsafe %s identifier mutation', (_kind, mutate) => {
    const fixture = mutableFixture();
    mutate(fixture);

    expect(() => assertPublicFixtureSafe(fixture)).toThrow(/namespace/);
  });

  it.each([
    ['release simulation flag', (fixture: MutableRecord) => {
      releaseOf(fixture).simulated = false;
    }],
    ['release execution mode', (fixture: MutableRecord) => {
      releaseOf(fixture).executionMode = 'Live execution';
    }],
    ['run simulation flag', (fixture: MutableRecord) => {
      const candidate = record(distributionsOf(fixture)[0].candidate, 'candidate');
      candidate.simulated = false;
    }],
    ['run execution mode', (fixture: MutableRecord) => {
      const candidate = record(distributionsOf(fixture)[0].candidate, 'candidate');
      candidate.execution_mode = 'live';
    }],
    ['run role', (fixture: MutableRecord) => {
      const candidate = record(distributionsOf(fixture)[0].candidate, 'candidate');
      candidate.role = 'baseline';
    }],
    ['decision relationship', (fixture: MutableRecord) => {
      distributionsOf(fixture)[0].decision_id =
        `${PUBLIC_FIXTURE_DECISION_ID_PREFIX}other-001`;
    }],
    ['deterministic evaluator', (fixture: MutableRecord) => {
      const gate = record(gatesOf(fixture)[0].gate, 'gate');
      const aggregate = record(gate.aggregate, 'aggregate');
      const evaluator = record(aggregate.evaluator, 'evaluator');
      evaluator.name = 'remote-evaluator';
    }],
  ])('rejects an unsafe %s mutation', (_kind, mutate) => {
    const fixture = mutableFixture();
    mutate(fixture);

    expect(() => assertPublicFixtureSafe(fixture)).toThrow(/Unsafe public fixture/);
  });

  it('rejects non-deterministic JSON values and cycles', () => {
    const dynamicFixture = mutableFixture();
    releaseOf(dynamicFixture).generatedAt = new Date(0);
    expect(() => assertPublicFixtureSafe(dynamicFixture)).toThrow(
      /non-plain object/,
    );

    const nonFiniteFixture = mutableFixture();
    releaseOf(nonFiniteFixture).sample = Number.NaN;
    expect(() => assertPublicFixtureSafe(nonFiniteFixture)).toThrow(
      /non-finite number/,
    );

    const cyclicFixture = mutableFixture();
    releaseOf(cyclicFixture).cycle = cyclicFixture;
    expect(() => assertPublicFixtureSafe(cyclicFixture)).toThrow(
      /cyclic reference/,
    );
  });
});
