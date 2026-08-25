# ADR 0008: Bind One Project per Deployment and Minimize Telemetry

- Status: Accepted
- Date: 2026-08-25

## Context

The durable API owns sensitive evaluation payloads, aggregate evidence,
idempotency metadata, and worker coordination state. Loopback binding reduced
accidental exposure during earlier development, but it did not identify callers
or separate read, mutation, cancellation, and operational access. The service
needs a narrow authorization contract without implying that shared PostgreSQL
rows provide tenant isolation.

Operators also need request, queue, worker, recovery, target, and evaluator
visibility. Generic access logs and automatic instrumentation can capture raw
paths, query strings, headers, bodies, exception text, prompts, outputs, SQL,
identifiers, or unbounded label values. That would turn telemetry into a second,
less protected evaluation-data store.

HTTP work is asynchronous. A run or comparison may execute long after the
submission request has ended and after a different worker claims it. Treating
that execution as an ordinary child span misrepresents the durable queue, while
discarding all context makes safe causal diagnosis difficult.

## Decision

### One project is one deployment

One application deployment and its database own exactly one project. The strict
`control-plane-auth/v1` file declares that project and an ordered set of
principals. Each principal contains a bounded identifier, a unique sorted scope
set, and only a SHA-256 bearer digest. The raw bearer value is provisioned out of
band, uses the exact `cpk_` prefix followed by 43 URL-safe characters, and is
never stored in application configuration.

The authentication file is required at API startup and must be an absolute,
bounded, regular, non-symlink file. The boundary hashes presented credentials,
performs constant-time digest comparisons, and requires exactly one
`X-Project-ID` equal to the deployment project. The project header is a
fail-closed routing assertion. It is not a row-level tenant selector, and a
database must not be shared by different projects.

Authorization uses four explicit scopes:

- `control-plane:read` for protected reads;
- `control-plane:write` for ordinary mutations;
- `control-plane:cancel` for cancellation; and
- `observability:read` for metrics retrieval.

Authentication and authorization run before protected request-body parsing or
application work. Invalid or missing credentials return the stable
`authentication_required` response with a Bearer challenge. Wrong project or
insufficient scope returns `permission_denied`. Both responses are content-safe
and omit credential, digest, project, and principal data. Liveness, readiness,
OpenAPI, and local API documentation remain unauthenticated so process and
schema health can be checked without granting evidence access.

### Telemetry is allowlisted metadata

API and worker composition roots inject an `Observability` instance. Each
instance owns an isolated Prometheus registry and OpenTelemetry tracer provider;
neither runtime mutates global providers. Telemetry failures are suppressed at
the instrumentation boundary and cannot change request, job, recovery, or
evidence behavior.

API metrics use fixed methods, route templates, status classes, error codes,
authorization outcomes, and persisted queue, failure, and aggregate usage
values from one fixed-cardinality query. Worker metrics use fixed poll outcomes, job kinds,
durable results, recovery counts, and readiness. Unknown values collapse to
bounded fallbacks. The authenticated `/metrics` route renders only the API
registry. Worker instruments remain process-local because the current worker
runtime deliberately has no HTTP listener or host port.

Structured logs use `control-plane-log/v1` events with bounded operational
metadata. The production API disables generic access logging. Request and worker
events exclude raw paths, query strings, bodies, headers, prompts, expectations,
outputs, SQL, rows, authorization material, project and principal identity,
idempotency keys, request digests, cursors, database configuration, job and
worker IDs, lease tokens, and raw exception text.

The default trace exporter writes only fixed `trace.span.completed` JSON
envelopes containing allowlisted operation, kind, outcome, timing, and linkage
IDs. It discards every span attribute and event. External OTLP export is not
enabled by default.

### Durable work uses a span link

The API accepts at most one strict lowercase W3C `traceparent` version `00`
value. Invalid or duplicate values are ignored, and `tracestate` is not
propagated. The API creates a server span using only the route template, bounded
method and status, generated request ID, and optional stable error code. Caller
request IDs are ignored rather than reflected into responses or telemetry.

For a run or comparison submission, the active trace context is validated and
stored as private job coordination metadata. It is excluded from the semantic
request digest and every public model. The first idempotent insert owns the
stored context; a later exact replay does not rewrite it.

When a worker claims the job, it starts a new `worker.job` consumer span with at
most one W3C Link to the submission span. Deterministic execution creates
content-free `evaluation.run`, `evaluation.target.invoke`, and
`evaluation.evaluator.evaluate` spans under the worker span. Spans record no
exception events or evaluation content. Trace context is correlation metadata,
not authentication or evidence identity.

### Deployment controls remain layered

Compose retains loopback API binding, a portless worker, fixed non-root users,
read-only filesystems, dropped capabilities, private database networking, and
file-mounted secrets. The application does not terminate TLS and does not
implement distributed rate limiting. Any non-loopback deployment must add a
trusted TLS-terminating and rate-limiting edge while preserving one isolated
deployment and database for each project.

## Consequences

- Compromise of one principal is bounded by its explicit scopes, but a bearer
  credential remains replayable until the operator rotates it.
- Project isolation is operationally clear and reviewable. Supporting multiple
  projects requires separate deployments rather than a misleading header-based
  shared-database design.
- Default telemetry supports latency, failure, authorization, queue, recovery,
  and trace diagnosis without becoming an evidence export.
- Low-cardinality schemas intentionally limit ad hoc debugging. Deeper diagnosis
  must use controlled access to the authoritative database or artifact store,
  not temporary raw logging.
- Durable span links preserve causal association without claiming a parent-child
  lifetime across queueing, retry, crash recovery, or process boundaries.
- Worker metrics require a future authenticated export boundary before an
  external scraper can collect them.
- TLS lifecycle, distributed abuse controls, credential issuance and rotation,
  backup scheduling, and measured recovery objectives remain operator concerns.

## Rejected alternatives

### Use `X-Project-ID` as a shared-database tenant selector

A caller-controlled header cannot provide row-level isolation by itself. Every
query, uniqueness rule, migration, backup, and administrative path would need a
separately verified tenant model. Separate deployments match the implemented
schema and reduce the chance of an unsupported isolation claim.

### Store raw bearer credentials in configuration

Raw values in mounted configuration increase disclosure impact through backups,
diagnostics, or accidental commits. Digest-only matching is sufficient for
opaque high-entropy bearer credentials and narrows retained secret material.

### Enable generic framework access logs and automatic instrumentation

Generic instrumentation has broader, version-dependent field collection and can
record raw request targets or exception details. Manual fixed-schema events and
spans make the privacy contract explicit and testable.

### Make worker execution a child of the HTTP span

A durable queued job is not bounded by the request lifetime and can be retried
by another process. A W3C Link represents that causal relationship without
creating an inaccurate synchronous trace tree.

### Put trace context in semantic request identity

Correlation metadata does not alter evaluation semantics. Hashing it into the
idempotency digest would make two otherwise identical retries conflict and would
allow observability choices to change durable work identity.
