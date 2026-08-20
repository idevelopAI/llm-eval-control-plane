# ADR 0003: Use Canonical Semantic Content Digests

- Status: Accepted
- Date: 2026-08-20

## Context

Datasets and run results need stable identities across machines and harmless
transport differences. Hashing source files directly makes line order,
whitespace, object-key order, source paths, and authoring metadata part of the
identity. Ordinary JSON serialization is also insufficient because equivalent
objects can have different byte representations.

A digest contract must state both the canonical byte encoding and the exact
semantic fields it covers. Otherwise a value labelled immutable cannot be
independently reproduced or safely used as release evidence.

## Decision

JSON content is parsed strictly and serialized with RFC 8785 JSON Canonicalization
Scheme bytes before SHA-256 hashing. The public digest format is
`sha256:<64 lowercase hexadecimal characters>`.

The parser rejects duplicate object keys, BOM-prefixed text, non-finite numbers,
malformed JSON, invalid UTF-8 at file boundaries, and values outside the RFC 8785
domain. User strings are preserved exactly. Unicode normalization is an explicit
evaluator behavior, not a storage or hashing side effect.

Dataset identity covers this semantic envelope:

```json
{
  "cases": ["case semantic records in case-ID order"],
  "digest_schema": "dataset/v1"
}
```

Case records include the case ID, input, every scoring expectation, and sorted
slice labels. Dataset name, revision, source path, timestamps, JSONL line order,
formatting, and any declared digest are excluded. Therefore reordering or
reformatting the same reviewed cases does not change the digest.

Run-result identity covers resolved dataset, target, and evaluator references;
canonically ordered case evidence; metric observations and failures; aggregates;
target output, structured outcome, usage, and control-plane latency. It uses the
version label `run-result/v1` and excludes only the caller-selected run ID.
Latency is rounded to six decimal places before it enters the result. The offline
fixture injects a deterministic clock; live measured latency will intentionally
produce different result content.

Arrays whose order is not semantic are normalized before hashing:

- dataset cases by case ID;
- slice labels lexicographically;
- run evaluators by logical artifact key;
- case results by case ID;
- metric summaries by metric and evaluator key;
- case observations by evaluator key and metric;
- evaluator failures by evaluator key.

Stored run files use a separate `run/v1` envelope. They must equal their RFC 8785
serialization plus exactly one LF. Loading validates both the storage envelope
and the embedded domain result digest.

## Consequences

- Equivalent reviewed datasets have one portable identity independent of JSONL
  formatting and authoring order.
- Run IDs can change without changing the stable result-content digest.
- Digest changes are explainable as semantic evidence changes rather than
  incidental serialization changes.
- RFC 8785 numeric and Unicode constraints are part of the public compatibility
  boundary.
- Changing an included field, normalization rule, or projection requires a new
  digest-schema version and migration strategy.
- Stored outputs may contain sensitive content even though their filenames are
  opaque; digesting is an integrity mechanism, not encryption or redaction.
