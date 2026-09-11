import { describe, expect, it } from 'vitest';

import { releaseDecision } from '../test/release-evidence';
import { isReleaseDecision } from './validation';

const suite = {
  kind: 'suite',
  name: 'release/core',
  revision: 1,
  digest: `sha256:${'a'.repeat(64)}`,
};

describe('optional suite evidence provenance', () => {
  it('accepts historical and explicitly pinned release summaries', () => {
    expect(isReleaseDecision(releaseDecision)).toBe(true);
    expect(isReleaseDecision({ ...releaseDecision, suite: null })).toBe(true);
    expect(isReleaseDecision({ ...releaseDecision, suite })).toBe(true);
  });

  it.each([
    { ...suite, kind: 'target' },
    { ...suite, digest: null },
    { ...suite, digest: 'private-invalid-digest' },
    { ...suite, revision: 0 },
    { ...suite, revision: true },
    { ...suite, name: 'a'.repeat(129) },
    { ...suite, name: 'private name' },
    { ...suite, document: 'private canonical content' },
    { ...suite, credentials: 'private value' },
  ])('rejects malformed or expanded suite metadata: %j', (invalid) => {
    expect(isReleaseDecision({ ...releaseDecision, suite: invalid })).toBe(false);
  });
});
