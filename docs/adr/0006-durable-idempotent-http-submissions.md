# ADR 0006: Coordinate HTTP Submissions with Durable Semantic Idempotency

- Status: Accepted; execution ownership extended by ADR 0007
- Date: 2026-08-20

## Context

Evaluation runs and baseline comparisons can be expensive, and HTTP clients
must retry when a connection fails before they receive a response. Retrying a
mutation must not silently execute the same work twice or create two evidence
resources. Raw request-byte equality is insufficient because JSON member order
and omission of a field carrying its documented default do not change request
meaning.

Process-local locks cannot coordinate multiple API processes and disappear on a
restart. Separately writing job state and completed evidence can also publish an
impossible partial outcome: a succeeded job without evidence, or evidence whose
job still appears to be running.

When this decision was accepted in Phase 4, the executor ran inline in the API
process. A crash could occur after ownership was claimed and before a terminal
state was recorded. That historical limitation motivated the leased execution
protocol now specified by
[ADR 0007](0007-leased-workers-and-fenced-publication.md); the semantic
idempotency boundary in this decision remains in force.

## Decision

`POST /v1/runs` and `POST /v1/comparisons` require an `Idempotency-Key` header.
The key is an opaque, bounded identifier and is scoped by job kind. It is stored
as coordination metadata and must not contain credentials, prompts, customer
identifiers, or other sensitive values.

The application validates and resolves all available preconditions before
claiming work. It then hashes the validated effective request with model defaults
materialized. This semantic request digest excludes the idempotency key because
the key is independently stored and uniquely constrained. Equivalent JSON with
different member order, or with a default omitted versus explicitly supplied,
produces the same digest.

The PostgreSQL repository atomically begins a job and stores its canonical
resolved payload under the unique `(kind, idempotency_key)` namespace:

1. The insert winner creates exactly one queued job and immutable payload. The API
   does not invoke the executor or comparator.
2. An existing key with the same semantic digest returns the stored job and does
   not enqueue another payload.
3. An existing key with a different semantic digest returns a stable conflict.
4. A distinct collision in the proposed job or resource identity also returns a
   conflict rather than attaching work to an unrelated record.

A new job starts as `queued`. Its leased attempt lifecycle, cancellation states,
bounded retries, and recovery transitions are defined by ADR 0007. Only a failed
job contains a bounded safe error code. Raw exceptions and persistence details
are not stored in the job.

Successful completion uses one fenced database transaction to insert the
immutable run or release-decision record and move the associated job to
`succeeded`. The transaction verifies job kind, resource identity, active
attempt, private lease token, and expected state. An exact response-lost retry
may succeed only when the already stored canonical evidence matches; changed
evidence is an immutable conflict.

API responses expose versioned safe summaries and a job `Location` header.
Nonterminal replays return `202` with the stored job rather than enqueueing new
work. Terminal replays return `200` with the completed state. The durable
payload and evidence remain private, append-oriented PostgreSQL records.

## Consequences

- Client timeouts can be retried without enqueueing a second job or payload.
- Equivalent request forms share one idempotency result, while changed semantics
  cannot be hidden behind the same key.
- Concurrent API processes coordinate through a database uniqueness boundary and
  compare-and-set transitions rather than a process-local mutex.
- Evidence publication and successful job completion are atomic.
- Idempotency metadata and full evidence are sensitive database content even
  though default API responses are redacted.
- Semantic idempotency does not provide exactly-once execution. ADR 0007 adds
  recovery with at-least-once invocation and fenced, transactional evidence
  publication while preserving the same semantic digest and immutable evidence
  rules.
- The implemented deterministic executor is credential-free and simulated.
  This decision does not make its latency or usage values equivalent to
  live-provider measurements.

## Rejected alternatives

### Retry every request

Unconditional retry can duplicate model calls and evidence creation whenever a
response is lost. It cannot safely distinguish a request that never started from
one that completed before the network failed.

### Hash raw HTTP bytes

Byte hashing treats semantically equivalent JSON as different and makes harmless
serialization choices part of the public contract. It also fails to represent
validated defaults consistently.

### Store completed evidence separately from job completion

Two independent commits expose partial states during a failure. A single
transaction provides a reviewable invariant between terminal job state and its
immutable resource.

### Reinvoke a stranded running job on replay

Without a lease and recovery protocol, the API cannot prove that the previous
invocation stopped before producing an external effect. Phase 4 therefore
preserved the running state. ADR 0007 replaces that historical behavior with
bounded lease recovery and explicitly accepts at-least-once external invocation;
HTTP replay itself still never directly reinvokes work.
