# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Reproducible Python development environment with a committed `uv.lock`.
- Immutable artifact references and evaluation specifications.
- Target-independent, content-addressed evaluation suite versions that bind a
  resolved dataset, evaluator metric inventories, declared slices, fixed
  execution semantics, and release gates.
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
- A versioned FastAPI control plane for registering dataset revisions,
  submitting evaluation and comparison jobs, inspecting job state, and paging
  redacted run and release-decision summaries.
- Durable PostgreSQL records for datasets, jobs, runs, and release decisions,
  with SQLAlchemy repository ports and an initial Alembic migration.
- Atomic semantic idempotency claims for run and comparison submissions,
  compare-and-set job transitions, and transactional evidence insertion with
  terminal job completion.
- A credential-free deterministic API executor, stable `api-error/v1`
  responses, sanitized request IDs, strict JSON handling, and bounded derived
  comparison work.
- A deterministic committed OpenAPI v1 document and a drift-checking export
  command that does not require a database or credentials.
- A hardened local Compose stack with a one-shot migration service, exact-schema
  readiness, loopback API binding, and file-mounted PostgreSQL secrets.
- Immutable resolved run and comparison payloads stored atomically with queued
  jobs, plus a six-state lifecycle with bounded attempt budgets and availability.
- PostgreSQL leased-worker claims using database time and
  `FOR UPDATE SKIP LOCKED`, durable redacted attempt history, heartbeats, bounded
  transient retry backoff, and concurrent expired-lease recovery.
- Fenced transactional run and release-decision publication, cooperative
  cancellation, and response-lost completion retry without stale-worker writes.
- A production worker runtime with graceful shutdown, a private fixed-content
  readiness file, a recovery loop, scale-safe identity, and hardened Compose
  deployment with no exposed worker port.
- A credential-free PostgreSQL worker recovery gate covering competing claims,
  crash recovery, cancellation races, attempt exhaustion, and evidence fencing.
- Project-bound bearer authentication with digest-only configuration, exact
  `X-Project-ID` assertion, and separate read, write, cancellation, and
  observability scopes.
- Privacy-safe `control-plane-log/v1` API and worker events, an authenticated
  API Prometheus endpoint, isolated low-cardinality worker instruments, and
  dependency-injected OpenTelemetry tracing.
- Strict W3C request-context acceptance, durable private submission trace
  metadata, asynchronous worker spans linked to their submission span, and
  content-free deterministic run, target, and evaluator spans.
- A least-privilege security workflow with locked dependency audit, Ruff
  security analysis, full-history redacted secret scanning, container
  vulnerability and configuration gates, and CodeQL `security-extended`
  analysis.
- Weekly uv, GitHub Actions, and Docker dependency updates, an
  implementation-specific threat model, an incident and recovery runbook, and
  static deployment-hardening tests.
- A responsive release-evidence workspace with accessible slice lenses,
  release-gate review, bounded case evidence, and fixed score, latency, and
  usage distributions.
- A dedicated fixture-only production graph for the public synthetic Site,
  backed by an accepted access decision and an exact deployed-artifact release
  record.

### Changed

- The offline target supports versioned, validated per-case scenario overrides
  for deterministic candidate-regression evidence.
- Documentation now describes the implemented Phase 2 release workflow and
  clearly separates synthetic fixture measurements from performance claims.
- Run and release comparison evidence now records execution mode and rejects a
  baseline/candidate comparison across different modes.
- Documentation now distinguishes deterministic DataBridge mock evidence from
  local PostgreSQL replay and unexecuted live-model accuracy.
- The API is now a second composition root alongside the CLI; both depend on
  application protocols rather than concrete persistence or execution adapters.
- Local service documentation now distinguishes simulated deterministic worker
  evidence from live-model measurements and documents the at-least-once external
  invocation boundary.
- Dataset, run, and release-decision collection routes now read bounded indexed
  metadata projections without loading complete canonical evidence documents;
  detail reads continue to validate the stored document against its indexes.
- Run and comparison submission handlers now validate and enqueue only. New or
  nonterminal submissions return `202`, terminal replays return `200`, and
  leased workers execute the pinned payload asynchronously.
- Durable replay coordination now combines semantic HTTP idempotency with
  exactly-once successful evidence publication while explicitly retaining
  at-least-once target or provider invocation.
- API v1 is now an authenticated single-project boundary. One deployment and
  database own one project; the exact project header is a routing assertion and
  does not provide row-level multitenancy.
- Accepted or generated request trace context is stored on the first durable job
  without changing the semantic request digest. Exact idempotency replays retain
  the original trace link.
- Production access logging now uses fixed-schema application events rather than
  raw Uvicorn access lines. Telemetry accepts only bounded route, outcome,
  duration, safe-error, and trace metadata.
- The dashboard now uses a solid-fill visual system with responsive navigation,
  failed-first review flow, keyboard-preserving interactions, and an updated
  release screenshot.
- The hosted fixture is publicly reachable while search indexing and every
  hosted-live data path remain separately disabled.
- Local Compose and every database-backed CI gate now use PostgreSQL 18.6 with
  its parent-directory volume layout; the recovery runbook defines the required
  major-version migration for existing PostgreSQL 17 volumes.
- Coupled CodeQL Action updates are grouped, and unsupported dashboard tooling
  majors are held until their plugin and runtime contracts are compatible.

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
- API bodies require strict unencoded JSON and are bounded by raw size, nesting,
  collection cardinality, slice fan-out, comparison gates, and derived work.
- Default API summaries and errors omit raw cases, prompts, expectations,
  outputs, SQL, rows, database URLs, semantic request digests, and exception
  text.
- Public job and attempt contracts also omit canonical payloads, worker
  identities, lease tokens, idempotency keys, and private coordination metadata.
- Worker errors, readiness state, and configuration validation use fixed safe
  content; database engines hide parameters, and payloads or lease credentials
  never enter logs or health files.
- Compose keeps the API on loopback, drops container capabilities, uses
  read-only filesystems where practical, and obtains the database password from
  a gitignored mounted file rather than an environment value.
- Raw bearer credentials are never stored in runtime configuration; only strict
  SHA-256 digests are accepted from a bounded, regular, non-symlink file.
- Protected API operations fail closed on missing or malformed authentication,
  wrong project assertion, and insufficient scope without echoing credential,
  digest, project, principal, or request content.
- Logs, metrics, traces, span links, errors, and scanner output exclude prompts,
  outputs, SQL, rows, request bodies, authorization material, identity fields,
  idempotency metadata, lease data, raw cursors, and exception text.
- Security automation uses full-SHA Action pins, checksum-verifies the Gitleaks
  release, scans complete history with redaction, and grants CodeQL only the
  permission required to upload security events.
- The public build gate rejects live-dashboard modules, credential and model
  markers, persistence APIs, gradients, source maps, secrets, route handlers,
  and unexpected runtime bindings, then probes representative `/api` and `/v1`
  methods against the built server.
- The accepted public Site retains `private, no-store`, restrictive browser
  headers, `noindex` and `nofollow`, empty hosted environment and resource
  bindings, and zero application API or model requests across the verified
  interaction flow.
- The service still has no TLS termination, distributed request-rate
  enforcement, or row-level multitenancy. Non-loopback use requires an external
  trusted edge and a separately isolated deployment for every project.
