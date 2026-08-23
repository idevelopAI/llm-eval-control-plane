# ADR 0006: Coordinate HTTP Submissions with Durable Semantic Idempotency

- Status: Accepted
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

The Phase 4 executor runs inline in the API process. A crash may therefore occur
after ownership is claimed and before a terminal state is recorded. The design
must preserve that uncertainty instead of guessing whether an invocation
finished.

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

The PostgreSQL repository atomically begins a job under the unique `(kind,
idempotency_key)` namespace:

1. The insert winner receives ownership and is the only caller permitted to
   invoke the executor or comparator.
2. An existing key with the same semantic digest returns the stored job and does
   not invoke work again.
3. An existing key with a different semantic digest returns a stable conflict.
4. A distinct collision in the proposed job or resource identity also returns a
   conflict rather than attaching work to an unrelated record.

A new job starts as `queued`, transitions by compare-and-set to `running`, and
then transitions from `running` to either `succeeded` or `failed`. No other
transition is legal. Only a failed job contains a bounded safe error code. Raw
exceptions and persistence details are not stored in the job.

Successful completion uses one database transaction to insert the immutable run
or release-decision record and move the associated job to `succeeded`. The
transaction verifies job kind, resource identity, and expected `running` state.
An exact completion retry may succeed only when the already stored canonical
evidence matches; changed evidence is an immutable conflict.

API responses expose versioned safe summaries and a job `Location` header.
Queued or running replays return the stored job rather than starting new work.
Terminal replays return the already completed state. The durable evidence itself
remains append-only in PostgreSQL.

## Consequences

- Client timeouts can be retried without a second invocation after the original
  request has durably claimed its job.
- Equivalent request forms share one idempotency result, while changed semantics
  cannot be hidden behind the same key.
- Concurrent API processes coordinate through a database uniqueness boundary and
  compare-and-set transitions rather than a process-local mutex.
- Evidence publication and successful job completion are atomic.
- Idempotency metadata and full evidence are sensitive database content even
  though default API responses are redacted.
- The scheme does not provide exactly-once execution. The insert winner can stop
  while the durable job is `running`; an identical replay reports that state and
  deliberately does not invoke the work again.
- Phase 5 must add leased workers, expiry policy, and explicit recovery for
  stranded jobs. Recovery must preserve the same semantic digest and immutable
  evidence rules.
- The current deterministic executor is synchronous, credential-free, and
  simulated. This decision does not make its latency or usage values equivalent
  to live-provider measurements.

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
invocation stopped before producing an external effect. Automatic reinvocation
would turn uncertainty into a likely duplicate. Phase 4 preserves the running
state for later operator or worker recovery instead.
