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
- Immutable aggregate, gate, case-transition, and release-decision contracts.
- Baseline comparison that verifies artifact alignment, recomputes stored
  evidence, and calculates global and slice aggregates.
- Absolute threshold, regression-budget, and coverage release gates for both
  higher-is-better and lower-is-better metrics.
- JSON, Markdown, and JUnit reports plus CI-safe `compare` exit codes.
- A pinned 40-case bilingual release fixture and credential-free GitHub Action
  that proves a seeded safety regression is blocked.
- Explicit `offline_mock` and `live` execution modes covered by run and release
  evidence digests.
- Strict DataBridge v1.2.0 mock and bounded HTTP target adapters for query,
  clarification, and policy-refusal decisions.
- A composite DataBridge evaluator for interaction decisions, clarification,
  unsafe-query rejection, PostgreSQL parsing and safety, execution success,
  expected columns, and result-set equivalence.
- SQLGlot PostgreSQL parsing plus allowlisted schemas, tables, functions, and
  syntax before database replay.
- Fresh, bounded read-only PostgreSQL transactions with normalized JSON-safe
  scalar evidence and content-safe failure codes.
- A pinned 56-case English/German DataBridge fixture, strict mock responses,
  four deliberate interaction/quality/safety regression overrides, the 12
  upstream adversarial SQL probes, a synthetic PostgreSQL seed, and source
  provenance with artifact digests.
- A `llm-eval databridge run` command for deterministic mock execution and
  explicitly opted-in live requests against synthetic DataBridge databases.
- Pre/post normalized PostgreSQL content verification tied to the reviewed seed
  digest, plus an offline CI gate backed by a digest-pinned PostgreSQL image and
  a no-write, no-temporary-table role.

### Changed

- The offline target supports versioned, validated per-case scenario overrides
  for deterministic candidate-regression evidence.
- Documentation now describes the implemented Phase 2 release workflow and
  clearly separates synthetic fixture measurements from performance claims.
- Run and release comparison evidence now records execution mode and rejects a
  baseline/candidate comparison across different modes.
- Documentation now distinguishes deterministic DataBridge mock evidence from
  local PostgreSQL replay and unexecuted live-model accuracy.

### Security

- Local evaluation artifacts are ignored by Git and target output disclosure is
  opt-in for one explicitly selected case.
- Default release reports omit case inputs, expectations, target outputs, and
  absolute local storage paths; report files use create-once semantics.
- DataBridge live credentials and replay DSNs are resolved only from named
  environment variables; their values are excluded from target identities,
  summaries, and sanitized failures.
- DataBridge HTTP calls require an explicit synthetic-database confirmation,
  HTTPS by default, disabled redirects and proxy inheritance, verified TLS,
  bounded time and response size, and strict response parsing.
- DataBridge response normalization drops answers, returned rows and columns,
  request IDs, and provider timings before persistence; generated SQL remains
  sensitive evidence in the ignored local artifact store.
- Live configuration rejects a shared API-key/DSN environment reference;
  response manifests are size-bounded before allocation and must align exactly
  with the reviewed dataset.
