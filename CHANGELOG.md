# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Reproducible Python development environment with a committed `uv.lock`.
- Immutable artifact references and evaluation specifications.
- Deterministic metric gates with direction, threshold, and regression budget.
- RFC 8785 canonical JSON, strict JSONL datasets, and content-derived dataset
  identities.
- Provider-neutral target, evaluator, failure, case-result, metric-summary, and
  run-result contracts.
- A serial in-process runner with one invocation per case, sanitized failure
  continuation, canonical ordering, and coverage-aware aggregation.
- Exact, normalized-text, JSON-schema, numeric-tolerance, structured-refusal,
  latency, and usage evaluators.
- A credential-free deterministic target and synthetic clock for offline tests
  and demos.
- Atomic create-once local run persistence with owner-only POSIX permissions,
  opaque storage keys, bounded reads, and integrity validation.
- CLI commands to run offline datasets and inspect redacted run or case evidence,
  alongside JSON Schema inspection and specification validation.
- A normalized 100-case fixture with pinned dataset and result digests.

### Changed

- Documentation now describes the implemented Phase 1 evaluation spine and
  clearly separates synthetic fixture measurements from performance claims.

### Security

- Local evaluation artifacts are ignored by Git and target output disclosure is
  opt-in for one explicitly selected case.
