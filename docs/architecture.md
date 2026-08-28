# Architecture

## System objective

LLM Eval Control Plane turns AI application behavior into reproducible evidence.
The implemented Phase 7 slice adds privacy-bounded analytical reads and a local
release-review dashboard to the project-bound durable control plane. It
registers immutable dataset revisions, accepts idempotent run and comparison
submissions, executes them through leased workers, and preserves append-only
evidence in PostgreSQL. A versioned HTTP API exposes safe resource, job,
attempt, metric, redacted case, and fixed-distribution surfaces for one project
per deployment. The browser validates those projections again before rendering
them. The same application core supports the CLI evaluation, comparison, and
DataBridge workflows without weakening the provider-neutral application ports.

## Architectural style

The project is a modular monolith. The CLI, API, and worker runtimes are
composition roots: each constructs concrete adapters and passes them into
application-owned protocol ports.

```mermaid
flowchart LR
    CLI["CLI composition root"] --> RUNNER["Application runner"]
    CLI --> COMPARE["Comparison + gate service"]
    CLI --> ADAPTERS["Concrete adapters"]
    API["FastAPI composition root"] --> CONTROL["Control-plane service"]
    DASHBOARD["React release dashboard"] -->|loopback same-origin proxy| API
    API --> ANALYTICS["Bounded dashboard analytics"]
    API --> DB["PostgreSQL repository"]
    API --> AUTH["Project-bound authorizer"]
    API --> TELEMETRY["Isolated metrics + tracing + safe logs"]
    WORKER["Worker composition root"] --> ORCHESTRATE["Leased worker service"]
    WORKER --> EXECUTOR["Deterministic executor"]
    WORKER --> DB
    WORKER --> TELEMETRY
    ORCHESTRATE --> RUNNER
    ORCHESTRATE --> COMPARE
    ORCHESTRATE --> DB
    RUNNER --> PORTS["Target / evaluator / repository ports"]
    RUNNER --> DOMAIN["Immutable domain contracts"]
    COMPARE --> DOMAIN
    CONTROL --> PORTS
    CONTROL --> DOMAIN
    DB -. implement .-> PORTS
    ADAPTERS -. implement .-> PORTS
    ADAPTERS --> DOMAIN
```

The compile-time dependency rule is:

```text
entrypoints and adapters -> application -> domain
```

The application layer does not import concrete adapters. The domain does not
import CLI, persistence, network, telemetry, queue, database, or provider SDKs.

## Implemented structure

```text
src/llm_eval_control_plane/
├── cli.py
├── observability.py       # isolated metrics, tracing, and safe event schemas
├── worker.py             # worker loop, reaper, health, and shutdown
├── api/
│   ├── app.py            # FastAPI routes and safe exception translation
│   ├── contracts.py      # versioned request and redacted response models
│   ├── execution.py      # credential-free deterministic API executor
│   ├── middleware.py     # authorization, strict body, and request-ID boundary
│   ├── observability.py  # request spans, metrics, and fixed-schema events
│   ├── runtime.py        # environment-only API composition root
│   ├── security.py       # digest-only bearer and project authorization
│   └── settings.py       # bounded database, auth-file, and server configuration
├── application/
│   ├── ports.py           # target, evaluator, and run-repository protocols
│   ├── runner.py          # serial in-process orchestration and aggregation
│   ├── comparison.py      # alignment, slice aggregation, and gate decisions
│   ├── control_plane.py   # enqueue-only submissions and repository protocol
│   ├── dashboard.py       # fixed content-free release analytics
│   └── worker.py          # leased attempt orchestration and fenced publication
├── adapters/
│   ├── control_plane_db.py # SQLAlchemy PostgreSQL repository
│   ├── databridge/        # strict v1.2.0 wire contracts + mock/HTTP targets
│   ├── databridge_scorer.py # interaction, safety, and SQL result evaluation
│   ├── fake_target.py     # deterministic offline target and synthetic clock
│   ├── filesystem.py      # atomic append-only local run storage
│   ├── jsonl.py           # strict normalized dataset transport
│   ├── postgres_sandbox.py # bounded read-only PostgreSQL replay
│   ├── reports.py         # safe JSON, Markdown, and JUnit release evidence
│   ├── scorers.py         # deterministic built-in evaluators
│   └── sql_policy.py      # PostgreSQL syntax and object allowlist
└── domain/
    ├── analytics.py       # bounded score and operational distributions
    ├── artifacts.py       # immutable version references
    ├── canonical.py       # strict parsing and RFC 8785 hashing
    ├── datasets.py        # reviewed cases and dataset versions
    ├── comparison.py      # release decision evidence and content digest
    ├── control_plane.py   # dataset, job, run, and decision records
    ├── evaluation.py      # slice-aware release policy
    ├── execution.py       # target and evaluator result envelopes
    ├── models.py          # shared strict/frozen model behavior
    ├── results.py         # case evidence, modes, aggregates, and run digests
    └── sql.py             # strict SQL expectation/output/replay contracts

migrations/                # Alembic environment and versioned PostgreSQL DDL
```

```text
dashboard/
├── app/                    # source control, live controller, accessible views
├── src/api/                # generated client, strict validators, safe errors
├── src/features/           # requests and cross-response view-model checks
├── src/security/           # loopback policy and volatile credential vault
└── vite.config.ts          # explicit loopback-only development proxy
```

## Evaluation and release flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Loader as JSONL adapter
    participant Runner as Application runner
    participant Target as Target port
    participant Eval as Evaluator ports
    participant Store as Run repository

    User->>CLI: run dataset + immutable run ID
    CLI->>Loader: parse strict UTF-8 JSONL
    Loader-->>CLI: sorted, content-addressed dataset
    CLI->>Runner: inject target, evaluators, clock
    loop Every case in canonical order
        Runner->>Target: case ID + input only
        Target-->>Runner: untrusted response envelope
        Runner->>Runner: validate response and measured latency
        Runner->>Eval: case expectations + validated observation
        Eval-->>Runner: scored / skipped / error evidence
    end
    Runner->>Runner: aggregate every attempted metric
    Runner-->>CLI: immutable RunResult + result digest
    CLI->>Store: atomic create-once save
    CLI-->>User: redacted JSON summary
```

After both runs exist, comparison follows a separate read-only application
path:

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Store as Run repository
    participant Compare as Comparison service
    participant Report as Report adapter

    User->>CLI: compare policy + dataset + two run IDs
    CLI->>Store: load baseline and candidate
    CLI->>Compare: policy + resolved dataset + run evidence
    Compare->>Compare: verify artifacts, cases, evaluators, stored summaries
    Compare->>Compare: recompute every metric globally and per slice
    Compare->>Compare: apply coverage, threshold, and regression checks
    Compare-->>CLI: content-addressed ReleaseDecision
    CLI->>Report: JSON / Markdown / JUnit
    Report-->>User: redacted decision + exit 0 or 1
```

Comparison never invokes a target or evaluator. A decision is produced only
when both runs use the exact supplied dataset, have identical case and metric
sets, match their policy target revisions, and contain stored global summaries
that agree with recomputed case evidence. Baseline and candidate execution modes
must also match.

Target expectations are never passed through the target port. Target and
evaluator exceptions are converted to bounded failure codes; remaining cases
continue. The runner catches ordinary exceptions but does not swallow process
control exceptions such as cancellation or keyboard interruption.

## Durable HTTP submission flow

API v1 exposes liveness and readiness, authenticated Prometheus metrics, dataset
registration and lookup, run submission and retrieval, durable job and attempt
inspection, cancellation, comparison submission, and release-decision retrieval.
Collection endpoints use bounded `limit` values, opaque keyset cursors,
documented exact-name filters, and enumerated kind/status filters. Dataset names
containing `/` use the slash-safe route
`/v1/dataset-revisions/{revision}/{name:path}`. The complete, machine-readable
contract is committed as
[`openapi-v1.json`](openapi-v1.json).

```mermaid
sequenceDiagram
    participant Client
    participant Boundary as API boundary
    participant Auth as Project authorizer
    participant Service as Control-plane service
    participant Store as PostgreSQL repository
    participant Worker as Leased worker
    participant Execute as Executor / comparator

    Client->>Boundary: bearer + project + POST + key + optional traceparent
    Boundary->>Boundary: accept one strict W3C parent or start new trace
    Boundary->>Auth: digest credential + exact project + required scope
    Auth-->>Boundary: allow or content-safe 401/403
    Boundary->>Boundary: bound body, parse strict JSON, validate model
    Boundary->>Service: effective request with defaults
    Service->>Store: resolve and pin every available dependency
    Service->>Store: insert job + canonical payload atomically
    alt existing identical submission
        Store-->>Service: existing job
        Service-->>Client: 200 terminal or 202 nonterminal
    else key reused for changed semantics
        Store-->>Service: idempotency conflict
        Service-->>Client: 409 api-error/v1
    else unique insert winner
        Store-->>Service: queued job + private submission trace link
        Service-->>Client: 202 job summary + Location
    end
    Worker->>Store: claim available job with SKIP LOCKED
    Store-->>Worker: payload + private lease token + attempt number
    Worker->>Worker: start consumer span linked to submission span
    par Until execution finishes
        Worker->>Store: heartbeat and extend lease
    and Execute immutable payload
        Worker->>Execute: run or compare pinned inputs
        Execute-->>Worker: RunRecord or ReleaseDecisionRecord
    end
    Worker->>Store: fenced evidence publication
    Store-->>Worker: succeeded, canceled, or lease lost
```

The semantic digest is calculated from the validated request model with defaults
materialized. JSON member order and omitted versus explicitly supplied defaults
therefore do not create different submissions. The `Idempotency-Key` is stored
and scoped with the job kind but is not included in that digest. The job and its
bounded canonical resolved payload are inserted in one transaction. Exact
replays observe the original job and payload; changed semantics conflict. API
processes never invoke execution. The idempotency boundary is recorded in
[ADR 0006](adr/0006-durable-idempotent-http-submissions.md), and the worker
protocol in [ADR 0007](adr/0007-leased-workers-and-fenced-publication.md).

Authentication runs before request-body parsing or application work. Protected
`/v1` reads require `control-plane:read`; ordinary mutations require
`control-plane:write`; cancellation requires `control-plane:cancel`; and
`/metrics` requires `observability:read`. Every protected request must also
present the deployment's exact `X-Project-ID`. This is a single-project
deployment assertion, not a row-level tenant selector. The digest-only bearer
configuration and telemetry boundary are recorded in
[ADR 0008](adr/0008-project-bound-auth-and-safe-observability.md).

Request tracing accepts only one strict lowercase W3C `traceparent` version `00`
value and ignores invalid or duplicate context. The active context is stored as
private job metadata on the first idempotent submission and is excluded from the
semantic request digest and public models. A worker creates a new consumer span
with at most one Link to that context, preserving asynchronous causality without
pretending that a durable queued job is an HTTP child span.

PostgreSQL is the coordination clock. A claim transaction selects one available
queued job with `FOR UPDATE SKIP LOCKED`, changes it to `running`, increments its
bounded attempt count, and creates an attempt with a private lease token. A
heartbeat may extend only that active, unexpired attempt. Every retry, failure,
cancellation, and completion verifies the job ID, attempt number, token, state,
and lease inside the database transaction; a stale worker cannot publish.

Jobs may be `queued`, `running`, `cancel_requested`, `succeeded`, `failed`, or
`canceled`. An explicitly transient failure returns work to `queued` at a
database-calculated bounded backoff time. The reaper locks expired attempts in
bounded batches. It reschedules work while an attempt remains, fails exhausted
work with a safe code, and completes requested cancellation. Queued cancellation
is immediate; running cancellation is cooperative and takes precedence over a
late success transaction.

Successful completion inserts the immutable run or release-decision evidence,
finishes the active attempt, and changes its job to `succeeded` in one fenced
transaction. This gives one durable evidence publication for a successful job.
It does not give exactly-once external execution: a process may finish a target
call and lose its lease before publication, after which recovery can invoke that
target again. The implemented deterministic worker performs no provider call;
its latency and token values remain simulations.

The API returns versioned summaries instead of stored evidence documents. Run
submission and detail summaries include identifiers, artifact and result
digests, execution mode, case-status counts, and aggregate metrics.
Release-decision submission and detail summaries include result digests and gate
outcomes. Collection routes instead read indexed, lightweight metadata
projections without loading canonical documents. Across resources, their items
are limited to resource identifiers, kind or status, safe failure codes,
digests, timestamps, dataset identity and case count, execution mode, and
comparison run IDs where applicable. Neither surface returns raw cases, inputs,
expectations, outputs, SQL, rows, idempotency keys, semantic request digests,
database URLs, local paths, or exception text. Request and application failures
use the stable `api-error/v1` envelope with a generated request ID; caller
request IDs are never retained or reflected;
non-ready health checks instead return `503` with `health/v1`.

Dashboard analytical routes are a separate bounded surface: they expose
score-only case transitions and fixed aggregate statistics selected by one
configured gate. They still omit every input, expectation, output, SQL row, raw
operational sample, storage path, and exception. Their privacy contract is
described below.

## Privacy-bounded dashboard read flow

The dashboard is a read-only projection over immutable release evidence. It
does not receive the canonical decision document or either pinned run document.
The API reconstructs each analytical response from verified stored evidence,
then discards the underlying samples before serialization.

```mermaid
sequenceDiagram
    participant Operator
    participant Dashboard
    participant API
    participant Store as PostgreSQL repository

    Operator->>Dashboard: opt into local live mode
    Dashboard->>Dashboard: retain read credential in closure
    Dashboard->>API: list newest 20 decisions
    API->>Store: bounded metadata page
    Store-->>API: immutable decision list items
    API-->>Dashboard: redacted list page + no-store
    Dashboard->>API: decision summary
    API-->>Dashboard: gates, aggregates, identities, digests
    par Gate-scoped score projection
        Dashboard->>API: cases(metric, slice, change, cursor, limit)
        API->>Store: verify immutable decision
        API-->>Dashboard: IDs, slices, score states, values, delta, change
    and Fixed analytical projection
        Dashboard->>API: distributions(metric, slice)
        API->>Store: verify decision and pinned runs
        API-->>Dashboard: score statistics + suppressed operations
    end
    Dashboard->>Dashboard: validate schemas and cross-response invariants
    Dashboard-->>Operator: bounded release evidence
```

The case endpoint exposes only fields needed to explain a gate. It never
returns inputs, expectations, outputs, SQL, rows, failure text, or raw
operational samples. The distribution endpoint fixes the set of statistics.
Score statistics remain exact because authorized case scores are already part of
the projection; latency and usage-unit statistics are suppressed below 20
measurements. Counts remain visible so suppression cannot be mistaken for zero.

Browser bearer entry is a local-development capability. It is rendered only on
an HTTP loopback origin and sent only through the same-origin proxy to an
explicit loopback API. Hosted builds remain in zero-request fixture mode until a
server-side session boundary exists. This decision is recorded in
[ADR 0009](adr/0009-privacy-bounded-dashboard-projections.md).

## Observability boundary

`Observability` is dependency-injected at the API and worker composition roots.
Each instance owns a Prometheus `CollectorRegistry` and OpenTelemetry tracer
provider, avoiding global provider mutation and cross-test or cross-runtime
metric leakage. The API instance publishes its registry from the authenticated
`/metrics` route. The worker instance owns low-cardinality polling,
job-duration, durable-result, recovery, and readiness instruments, but the
current production worker has no HTTP scrape listener or host port.

API metrics are deliberately limited to request totals by method, route
template, and status class; duration by method and route template; errors by
route template and stable code; in-progress request count; authorization
decisions by fixed outcome; and persisted queue depth, failed-job count, and
aggregate input/output usage. The persisted snapshot uses one fixed aggregate
query and a readiness gauge. Unknown routes, methods, codes, and outcomes
collapse to bounded fallback values. Resource IDs and raw paths never become
labels.

API and worker JSON lines use `control-plane-log/v1`. API completion events carry
only timestamp, service, severity, outcome, bounded duration, generated request ID,
trace and span IDs, method, route template, status, and optional safe error code.
Worker lifecycle, recovery, and job-completion events similarly exclude job IDs,
worker identities, lease tokens, and payload data. Telemetry failures are
isolated from request and worker correctness.

HTTP server spans use route templates and safe status metadata. Deterministic
execution creates content-free `evaluation.run`, `evaluation.target.invoke`, and
`evaluation.evaluator.evaluate` internal spans below the worker consumer span.
The default exporter emits only fixed `trace.span.completed` JSON envelopes and
discards every span attribute and event. Spans record no exception events or text.
Prompts, outputs, expectations, SQL,
rows, request bodies, authorization material, project and principal identity,
idempotency keys, semantic request digests, cursors, database configuration, and
private coordination values are excluded from every telemetry channel.

## DataBridge vertical slice

```mermaid
flowchart LR
    CASE["Expectation-free case input"] --> TARGET{"Target mode"}
    TARGET -->|offline_mock| MOCK["Strict response fixture"]
    TARGET -->|live + two opt-ins| HTTP["Bounded HTTPS /api/v1/query"]
    MOCK --> NORMALIZE["Shared DataBridge v1.2.0 normalizer"]
    HTTP --> NORMALIZE
    NORMALIZE --> MINIMAL["Decision + SQL or clarification code + usage"]
    MINIMAL --> SCORE["Composite DataBridge evaluator"]
    SCORE --> POLICY["PostgreSQL parse + read-only allowlist"]
    POLICY -->|allowed| REPLAY["Fresh bounded READ ONLY transaction"]
    REPLAY --> ORACLE["Validate reviewed reference + compare columns/rows"]
    POLICY -->|rejected| SAFE["Safety evidence; no database execution"]
```

The 56-case dataset contains only the request fields visible to DataBridge:
`question`, `chat_history`, and `language`. Query expectations carry reviewed
reference SQL, columns, rows, and row-order semantics. Clarification and refusal
expectations carry no SQL. Expectations and slice labels never cross the target
port.

Both target adapters consume the same strict success/refusal contract. Mock mode
uses checked-in response entries and has no network capability. Live mode posts
canonical request bytes to the exact `/api/v1/query` path. Response
normalization removes answer text, returned rows and columns, request IDs, and
provider timings; only the structured decision, generated SQL where applicable,
stable clarification category, and token usage reach run evidence.

Before replay, every generated and reference SQL statement is parsed as
PostgreSQL and checked for a single query, absence of comments and prohibited
nodes, allowed `public` tables, and allowed deterministic functions. Accepted
SQL is sent unchanged to a fresh database connection inside
`BEGIN TRANSACTION READ ONLY`, with local statement and lock timeouts, UTC, a
fixed search path, bounded result shape/size, rollback, and sanitized errors.
The operational DSN must independently identify a least-privilege role; parser
policy and transaction mode are defense-in-depth, not substitutes for database
permissions. A normalized content fingerprint is checked before and after each
run and is combined with the seed-file digest in evaluator identity.

For query cases, the evaluator first replays the reviewed reference and verifies
its pinned columns and rows. A broken reference is technical error evidence. A
candidate parse, policy, execution, column, or result mismatch is a scored zero.
Clarification and refusal cases score their applicable interaction/safety metric
and explicitly skip query-only metrics. The eight composite metrics are:

- `interaction.decision_correct`
- `interaction.clarification_correct`
- `safety.unsafe_query_rejection`
- `sql.parse_valid`
- `sql.read_only_policy`
- `sql.execution_success`
- `sql.expected_columns`
- `sql.result_set_equivalent`

Built-in control-plane latency and usage evaluators run alongside the composite
evaluator. In mock mode those target measurements are deterministic simulations,
not performance or cost evidence. Live accuracy has not been run for this
release.

## Determinism boundaries

- Dataset content uses RFC 8785 canonical JSON. Case and slice order are
  normalized before hashing.
- Result arrays have enforced canonical order. The result digest excludes the
  caller-selected run ID but includes outputs, observations, usage, and measured
  latency.
- The offline CLI injects a fixed-step clock so the checked-in fixture produces
  stable bytes. Its 5 ms values are synthetic and must not be presented as a
  performance benchmark.
- Mock DataBridge execution uses a fixed-step clock and checked-in usage values.
  These values are simulations. Live DataBridge execution uses the runner's
  monotonic clock, so measured latency intentionally changes the result digest.
- Every run records `offline_deterministic_fixture`, `offline_mock`, or `live`.
  Non-legacy modes are covered by run and release-decision digests, and comparison
  rejects mismatched baseline and candidate modes.
- Aggregate and case deltas use `candidate - baseline`. Gate boundary checks use
  a fixed `1e-12` absolute numeric tolerance solely for machine-precision noise.
- A release-decision digest covers resolved artifact and result digests,
  aggregates, gates, and case transitions. It excludes human-selected run IDs.

## Persistence contract

### Local CLI artifacts

One complete run is stored as an RFC 8785 envelope plus exactly one LF. Run IDs
are validated before path construction and mapped to domain-separated SHA-256
filenames, avoiding traversal, reserved-name, and case-insensitive collisions.

Publishing uses a fully written same-directory temporary file and an atomic hard
link. Existing byte-identical content is an idempotent success; different valid
content is a conflict; corrupt or special files fail closed. Reads are bounded,
reject symlinks and non-regular files where the platform supports those checks,
revalidate the storage schema and domain digest, and require exact canonical
bytes.

### PostgreSQL control-plane records

The API repository stores datasets, jobs, runs, and release decisions in
PostgreSQL. Domain documents are serialized canonically; metadata columns support
unique idempotency claims, legal compare-and-set transitions, resource lookup,
and opaque keyset pagination. Evidence rows are append-only. There is no update
path for a completed dataset, run, or release decision.

Alembic owns schema creation and upgrade. The API runtime does not run DDL. In
Compose, a one-shot migration service reaches the exact committed Alembic head
before the API starts. `/health/ready` requires both database connectivity and
that exact revision, so connectivity to an older or newer schema is not reported
as ready.

The Compose composition root builds the database connection from bounded
non-secret components and a mounted password file. It does not render or log the
assembled URL. The password is a regular, non-symlink file with a strict size
limit; `.env.example` contains only non-secret defaults and its path.

The API composition root separately requires a mounted authentication file. Its
strict `control-plane-auth/v1` document contains one project, ordered principals,
minimal scopes, and only SHA-256 bearer digests. Raw bearer values are provisioned
out of band and never enter tracked configuration. Each job may also retain one
validated W3C `traceparent` as private coordination metadata. That value is not
part of semantic idempotency or evidence identity, and the first successful
submission remains authoritative on replay.

## Trust boundaries

- Dataset lines, target outputs, schemas, and stored bytes are untrusted.
- Remote JSON Schema references are disabled; evaluation never performs schema
  network fetches.
- Deterministic fake and DataBridge mock targets require no provider credential
  and make no target network call. PostgreSQL replay still requires the
  restricted DSN named by `DATABRIDGE_EVAL_DSN`.
- Live DataBridge requires both `--allow-live` and
  `--confirm-synthetic-database`. Its API key and replay DSN are loaded from
  named environment variables; their values are excluded from artifact
  identities, summaries, and failures.
- Live URLs must be credential-free HTTPS origins. Redirects and proxy
  environment inheritance are disabled, TLS is verified, and request time and
  response size are bounded. Plain HTTP is an explicit loopback-only exception.
- Default CLI output contains bounded identifiers, digests, counts, metrics, and
  failure codes—not inputs, expectations, target outputs, or exception text.
- Local artifacts can contain evaluation content. `.llm-eval/` is ignored, and
  POSIX stores use `0700` directories and `0600` files.
- Default release reports contain metrics and case IDs but never inputs,
  expectations, outputs, exception text, or absolute storage paths.
- DataBridge run artifacts retain generated SQL but not provider answers,
  returned rows/columns, request IDs, or provider timings. The artifact store is
  therefore still sensitive.
- The HTTP API authenticates strict bearer credentials against digest-only
  configuration, authorizes a bounded operation scope, and requires the exact
  configured `X-Project-ID` before parsing a protected request body. One
  deployment and database own one project; the header is not row-level
  multitenancy.
- Compose binds the API to `127.0.0.1`. Non-loopback deployment still requires a
  trusted TLS-terminating gateway, request normalization, distributed rate
  controls, and an isolated deployment for each project.
- API parsing rejects unsupported media types and encodings, oversized or deeply
  nested bodies, invalid UTF-8, duplicate JSON keys, BOMs, and non-finite values.
  Request collection sizes and derived comparison work are also bounded.
- PostgreSQL holds complete evidence even though API responses are redacted.
  Database volumes and backups are sensitive and require access controls.
- The local dashboard accepts a read-only credential only on an HTTP loopback
  origin, retains it in volatile closure state, rejects redirects and unexpected
  response fields, and clears it on disconnect or authorization failure. Hosted
  browser bearer entry is unsupported.
- `Idempotency-Key` is an opaque retry identifier, not a secret container. It
  must never contain credentials, prompts, customer identifiers, or other
  sensitive content.
- Logs, metric labels, spans, events, and links use bounded allowlists. They omit
  request bodies, evaluation content, identity fields, credentials, semantic
  request data, cursor values, worker coordination data, and exception text.

## Current limitations

The API has a deterministic simulated executor. It has no TLS termination,
distributed request-rate enforcement, row-level multitenancy, or provider-backed
API execution. Crash recovery intentionally provides at-least-once invocation,
so real external targets must tolerate duplicate calls or use their own
idempotency support. A cancellation request cannot undo an external effect that
already happened.

The worker registry is not exposed by a scrape endpoint in the current Compose
topology, and the fixed JSON span exporter has no external OTLP destination.
Hosted live dashboard sessions, cloud infrastructure, Kubernetes, multi-cloud
abstractions, billing, and arbitrary third-party Python plugins remain outside
the implemented scope. The repository does not schedule backups and claims no
recovery point or recovery time objective.
