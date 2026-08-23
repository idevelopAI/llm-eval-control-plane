# ADR 0007: Execute Durable Jobs with Leases and Fenced Publication

- Status: Accepted
- Date: 2026-08-23

## Context

ADR 0006 made HTTP submissions semantically idempotent, but its Phase 4 API
process executed work inline. A process could stop after changing a job to
`running` and before recording evidence, leaving no safe way to distinguish an
unfinished call from an external effect whose response was lost.

Phase 5 needs asynchronous workers that can compete for work, survive process
loss, bound retries, expose useful attempt history, and support cancellation. A
late or partitioned worker must never publish after another worker has recovered
the job. PostgreSQL is already the durability boundary, and introducing a
separate broker would create another source of truth before the workload needs
one.

## Decision

### Submission and payload durability

The API resolves every available dependency and validates execution contracts
before enqueueing. The repository inserts one queued job and its size-bounded
RFC 8785 canonical payload in the same transaction. Payload kind, digest, and
embedded resource identities are validated on load. An exact idempotency replay
returns the original job and payload; changed semantics conflict. Terminal
legacy jobs may omit a payload, but migrated unfinished legacy jobs fail closed
with `legacy_payload_missing` rather than executing guessed input.

Submission handlers never invoke a target, evaluator, or comparison. New and
nonterminal submissions return `202`; terminal replays return `200`. Both expose
the job location.

### Claims, leases, and attempts

PostgreSQL is the authoritative coordination clock. A worker claim uses one
transaction and `FOR UPDATE SKIP LOCKED` to select the oldest available queued
job without blocking other workers. The transaction changes the job to
`running`, increments its bounded attempt count, and inserts one attempt row with
its private worker identity, fresh high-entropy lease token, heartbeat time, and
lease expiry.

Only one running attempt may exist for a job. Heartbeats extend an active,
unexpired lease using database time. Every heartbeat, retry, failure,
cancellation acknowledgement, and completion is fenced by the job ID, attempt
number, private token, expected job and attempt state, and current lease. Missing,
expired, or superseded ownership reports lease loss and cannot mutate durable
state.

The public API exposes attempt number, status, safe failure code, and timestamps.
It never exposes the payload, idempotency key, semantic request digest, worker
identity, or lease token.

### Retry and recovery

Only an explicit transient execution signal is automatically retried during a
live attempt. Retry changes the attempt to `retry_scheduled` and the job back to
`queued`, with a database-calculated exponential delay capped by configured
bounds. Other ordinary execution failures terminate the job with a stable safe
code.

The worker runtime periodically reaps expired attempts in bounded batches using
row locks and `SKIP LOCKED`. If another attempt remains, it marks the expired
attempt `lease_expired` and reschedules the job with the same bounded backoff. If
the attempt budget is exhausted, it fails the job. A `cancel_requested` job is
canceled instead of retried. Multiple workers may reap concurrently without
recovering one attempt twice.

### Cancellation and completion

A cancellation request changes queued work directly to `canceled`. Running work
changes to `cancel_requested`; its attempt remains active until the worker
acknowledges cancellation or the reaper observes lease expiry. Repeated requests
for an already canceled job are idempotent. A succeeded or failed job remains
immutable and returns a conflict.

Successful completion inserts immutable run or release-decision evidence,
changes the active attempt to `succeeded`, and changes the job to `succeeded` in
one fenced transaction. If cancellation was requested first, cancellation wins
and no evidence is inserted. A response-lost retry using the same active token
may confirm only byte-identical evidence; changed evidence conflicts. A stale
token can never use this retry path.

### Delivery guarantee

The protocol provides at-least-once target or provider invocation. A process can
complete an external call and lose its lease before publishing, so recovery may
repeat the call. The fencing transaction provides exactly one immutable evidence
publication for a successfully completed job. It does not provide exactly-once
external side effects, and cancellation cannot undo an effect that already
occurred.

## Consequences

- API latency is decoupled from evaluation duration, and API restarts do not own
  execution progress.
- Multiple workers can claim and reap concurrently through PostgreSQL without a
  process-local mutex or duplicate active attempt.
- Expired work is recoverable, retries are delayed and bounded, and every attempt
  has a durable redacted history.
- Database time avoids ownership decisions based on skewed worker clocks.
- Transactional fencing prevents a stale worker from overwriting cancellation,
  recovery, or successful evidence.
- Payload and attempt tables contain sensitive evaluation and coordination data;
  access, backups, logs, and diagnostics must preserve that boundary.
- Provider integrations that cannot tolerate repeated external effects must add
  provider-side idempotency keyed to a stable resource identity.
- The current PostgreSQL repository is required for claim and reaper operations;
  SQLite remains useful only for portable non-concurrency tests.

## Rejected alternatives

### Continue executing inside the API process

Inline work couples request lifetime to evaluation duration and cannot safely
recover a process that stopped between an external effect and durable evidence.

### Treat a `running` status as sufficient ownership

A status-only compare-and-set cannot distinguish the original worker from a
replacement after recovery. Attempt number and a private lease token are needed
to fence every mutation.

### Let worker clocks set lease timestamps

Clock skew can make two workers disagree about ownership. Database-generated
timestamps keep claims, heartbeats, retry availability, cancellation, and expiry
on one authoritative clock.

### Claim with an unlocked select

Selecting and then updating in separate coordination steps allows competing
workers to observe the same queued job. A locked PostgreSQL claim with
`SKIP LOCKED` makes the ownership change atomic while allowing parallel progress.

### Promise exactly-once provider execution

No database transaction can atomically commit an arbitrary external provider
effect and local PostgreSQL evidence. Claiming exactly-once execution would hide
the unavoidable crash window. The contract instead states at-least-once
invocation and exactly-once successful evidence publication.

### Introduce a message broker immediately

A broker would add delivery, redrive, credential, and split-brain concerns while
PostgreSQL already owns job state and evidence. The leased table queue is bounded
and sufficient for this local control-plane phase.
