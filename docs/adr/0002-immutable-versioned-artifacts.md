# ADR 0002: Use Immutable Versioned Evaluation Artifacts

- Status: Accepted
- Date: 2026-08-19

## Context

An aggregate score cannot be reproduced when its dataset, target configuration,
prompt, evaluator, or gate policy changes in place. Mutable names are convenient
for authoring but insufficient as evidence.

## Decision

Every reproducibility-relevant input is referenced by artifact kind, stable name,
positive revision, and optional SHA-256 digest. Published revisions are immutable;
changes create a new revision. A run stores fully resolved references.

Credentials are never part of an artifact snapshot. Target versions will contain
only a secret reference that is resolved at execution time.

## Consequences

- A run can be connected to the exact inputs that produced it.
- Storage will contain more artifact revisions instead of in-place edits.
- Canonical hashing rules are a public compatibility contract for
  content-derived versions; see ADR 0003.
