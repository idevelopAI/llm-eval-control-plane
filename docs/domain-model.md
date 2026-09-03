# Domain Model

## Vocabulary

| Concept | Meaning | Status |
|---|---|---|
| `ArtifactRef` | Immutable `(kind, name, revision, digest)` identity | Implemented |
| `EvaluationCase` | Reviewed input plus deterministic scoring expectations | Implemented |
| `DatasetVersion` | Sorted, content-addressed set of unique cases | Implemented |
| `TargetRequest` | Case ID and input visible to a target | Implemented |
| `TargetResponse` | Validated output, structured outcome, and required usage | Implemented |
| `TargetObservation` | Validated response plus control-plane latency | Implemented |
| `MetricObservation` | Explicit scored, skipped, or error evaluator evidence | Implemented |
| `CaseResult` | Target evidence and all evaluator outcomes for one case | Implemented |
| `MetricSummary` | Attempted/scored/skipped/error counts and optional mean | Implemented |
| `RunResult` | Complete immutable run with resolved artifacts and digest | Implemented |
| `ExecutionMode` | `offline_deterministic_fixture`, `offline_mock`, or `live` evidence boundary | Implemented |
| `EvaluationSpec` | Dataset, candidate, baseline, and slice-aware gate policy | Implemented |
| `MetricGate` | Directional threshold and absolute regression budget | Implemented |
| `MetricAggregate` | Attempted/scored/skipped/error evidence for one metric and slice | Implemented |
| `AggregateComparison` | Candidate, baseline, and candidate-minus-baseline aggregate | Implemented |
| `GateCaseComparison` | Threshold-relative case transition for one configured gate | Implemented |
| `GateResult` | Coverage, threshold, and regression checks for one gate | Implemented |
| `ReleaseDecision` | Content-addressed pass/fail evidence for the whole policy | Implemented |
| `DatasetRecord` | Immutable dataset revision plus its durable registration time | Implemented |
| `JobRecord` | Durable submission identity, semantic digest, resource ID, and lifecycle state | Implemented |
| `JobKind` | `run` or `comparison` idempotency namespace | Implemented |
| `JobStatus` | `queued`, `running`, `cancel_requested`, `succeeded`, `failed`, or `canceled` lifecycle state | Implemented |
| `JobPayload` | Canonical resolved run or comparison input pinned at submission | Implemented |
| `JobAttemptRecord` | Redacted timing, outcome, and safe failure metadata for one leased attempt | Implemented |
| Project boundary | One configured project owned by one deployment and database | Implemented |
| Principal | Bounded identity, digest-only bearer reference, and ordered scope set | Implemented |
| `ControlPlaneScope` | Read, write, cancellation, or observability permission | Implemented |
| `TraceParent` | Private strict W3C submission-to-worker correlation metadata | Implemented |
| `RunRecord` | Append-only run result plus its durable creation time | Implemented |
| `ReleaseDecisionRecord` | Append-only release decision plus stable ID and creation time | Implemented |
| `CursorPage` | Stable bounded page plus opaque continuation cursor | Implemented |
| `SqlExpectation` | Reviewed query, clarification, or refusal oracle | Implemented |
| `SqlTargetOutput` | Minimal normalized decision and generated-SQL evidence | Implemented |
| `SqlReplayResult` | Bounded normalized PostgreSQL columns and rows | Implemented |
| `SuiteEvaluator` | Executor binding, resolved evaluator identity, and exact metric inventory | Implemented |
| `SuiteExecutionSettings` | Adapter, execution mode, canonical order, single invocation, and serial concurrency | Implemented |
| `EvaluationSuiteVersion` | Target-independent, content-addressed evaluation protocol and release policy | Implemented |
| Experiment history | Derived suite-pinned runs and release decisions, not a separate mutable entity | Proposed |

The deterministic fake target and built-in scorers are adapter implementations,
not additional domain entities. Their `ArtifactRef` values identify their exact
behavior revisions inside a run.

## Canonical data invariants

- Models are frozen, reject unknown fields, and validate default values.
- Canonical JSON uses RFC 8785 bytes. Duplicate keys, malformed UTF-8, BOMs,
  non-finite numbers, and values outside the JCS domain are rejected.
- User strings are preserved exactly; global trimming or Unicode normalization
  is not applied to stored prompts or outputs.
- Dataset case IDs are unique and sorted. Slice labels are unique and sorted.
- A dataset digest covers `digest_schema` and semantic case records. Dataset
  name, revision, source path, file order, whitespace, and a declared digest are
  not part of its content identity.
- Every resolved artifact digest uses canonical
  `sha256:<64 lowercase hexadecimal characters>` form.

## Proposed suite-version invariants

- An `EvaluationSuiteVersion` has an author-facing name and positive revision,
  but its `evaluation-suite/v1` content digest excludes both values. Publishing
  changed semantic content requires a new revision.
- A suite is target-independent. It contains one resolved dataset reference,
  one or more `SuiteEvaluator` bindings, declared slices,
  `SuiteExecutionSettings`, and one or more release gates. Baseline and candidate
  targets remain separately resolved run artifacts.
- Each evaluator binding contains a bounded executor name, one resolved
  evaluator reference, and its complete nonempty metric inventory. Executor
  names, evaluator logical keys, and metric names are unique. Bindings and
  metric names have canonical order.
- Declared slices are unique, lexicographically ordered labels present in the
  resolved dataset. A sliced gate references a declared slice, and a gate metric
  is supplied by exactly one suite evaluator. Gates are unique and canonically
  ordered by metric and optional slice.
- Suite execution settings contain the adapter, exact execution mode, canonical
  case-ID order, one target invocation per case, and concurrency fixed to one.
  These settings describe semantic execution; worker leases, heartbeats,
  attempts, retry timing, and queue placement remain operational metadata.
- The suite digest covers the resolved dataset, evaluator bindings and metric
  inventories, declared slices, execution settings, and release gates with all
  defaults materialized. It excludes name, revision, registration time, source
  formatting, credentials, secrets, database configuration, and operational
  coordination settings.
- Once suite registration is implemented, an identical put at one
  `(name, revision)` will be idempotent and different content will conflict.
  Workers will consume the exact resolved suite snapshot pinned at submission;
  comparisons will reject different suite revisions or digests rather than
  treating them as a failed release.
- Experiment history is derived from immutable suite-pinned runs and the
  release decisions that connect exact baseline and candidate evidence. No
  separate experiment definition, table, mutable status, or current-result
  pointer is part of the proposed domain.

These contracts are proposed in
[ADR 0012](adr/0012-versioned-evaluation-suites.md). The frozen suite models,
canonical normalization, and digest calculation are implemented. Registration,
persistence, API and CLI surfaces, worker payload pinning, derived experiment
history, and run/decision digest integration are not implemented yet. Existing
evidence is legacy suite-unpinned evidence and is not assigned an inferred
suite.

## Execution invariants

- A target receives `case_id` and `input` only. It never receives `expected`,
  expected refusal state, schemas, tolerances, or slice labels.
- During one evaluation-runner invocation, each case is invoked exactly once.
  Worker crash recovery may repeat the whole invocation after an external effect
  but before durable publication; execution is therefore at least once.
- Target responses require structured refusal state and explicit non-negative
  input/output usage. Refusals are never inferred from wording.
- Every run records its execution mode. Deterministic fake runs default to
  `offline_deterministic_fixture`; DataBridge mock and live runs record
  `offline_mock` and `live` explicitly.
- The application boundary validates every untrusted target and evaluator return
  value.
- Missing or invalid target output produces a sanitized target failure and does
  not remove the case from aggregates.
- Evaluator exceptions become sanitized evaluator failures. No raw exception
  text is persisted.
- Each configured metric has exactly one scored, skipped, or error outcome per
  attempted case, including target failures.
- Control-plane latency uses an injected monotonic clock and is rounded to six
  decimal places. The offline fixture uses a synthetic fixed-step clock.

## Result invariants

- Evaluator references sort by logical key; case results sort by case ID; metric
  summaries sort by metric and evaluator key.
- Observations and evaluator failures inside each case also have canonical order.
- A metric summary satisfies
  `attempted = scored + skipped + errors`.
- A mean exists exactly when at least one observation was scored. Skipped and
  error observations are never silently dropped from coverage counts.
- `completed_with_failures` represents technical target/evaluator failures, not
  low metric values. Release policy is applied later by comparison.
- The result digest covers resolved artifacts, target evidence, observations,
  failures, aggregates, and non-legacy execution mode. It excludes only the
  caller-selected run ID.
- Loading a stored result recalculates and verifies its result digest.

## DataBridge SQL invariants

- DataBridge input is a strict object containing only `question`,
  `chat_history`, and `language` (`en` or `de`). Expected SQL, expected results,
  refusal state, and slices remain evaluator-only data.
- `SqlExpectation.behavior` is exactly `query`, `clarification`, or `refusal`.
  Query expectations require reviewed SQL, unique expected column names,
  expected rows, and `ordered` or `unordered` row semantics. They cannot contain
  clarification codes.
- Clarification expectations contain one or more accepted stable codes and no
  SQL evidence. Refusal expectations contain neither SQL evidence nor
  clarification codes. Explicit irrelevant fields are rejected even when their
  value is `null`.
- Normalized query output contains one to 16 SQL executions. Clarification
  output contains a stable code and no SQL. Refusal output contains neither.
  Natural-language answers, returned database rows and columns, request IDs, and
  provider timings do not enter `SqlTargetOutput`.
- The strict DataBridge wire response is bounded and category-consistent:
  `answered` requires at least one execution, while
  `clarification_required` permits none. HTTP `403` is normalized as structured
  refusal without parsing or retaining the response body.
- Every SQL execution is policy-checked before a database connection is opened.
  The PostgreSQL policy requires exactly one query, rejects comments and
  prohibited syntax, restricts schemas and tables, and allowlists deterministic
  functions. Policy-rejected SQL is never replayed.
- PostgreSQL replay uses the original accepted SQL in a fresh explicit read-only
  transaction with statement, lock, connection, row, column, cell, and encoded
  result limits. Rollback and connection close are attempted on every path.
- Evaluation starts only when the connected fixture matches its pinned
  normalized content fingerprint, rechecks that fingerprint before persistence,
  and covers it together with the seed-file digest in evaluator identity.
- PostgreSQL scalars normalize to canonical JSON-safe booleans, integers,
  finite floats, strings, `null`, ISO 8601 dates/times, and lowercase UUID text.
  Unsupported or non-finite values produce sanitized replay errors.
- The composite evaluator emits exactly one scored, skipped, or error
  observation for each of its eight metrics. Query candidate failures score
  zero; a broken reference SQL/fixture oracle produces technical error evidence;
  category-inapplicable metrics are explicitly skipped.
- Ordered result expectations compare canonical rows positionally. Unordered
  expectations compare row multisets, preserving duplicate counts.
- DataBridge mock response, latency, and usage evidence is deterministic and
  simulated. It does not establish deployed-model accuracy or performance. Live
  accuracy was not run for this release.

## Durable submission invariants

- A job has one stable ID, kind, status, opaque idempotency key, semantic request
  digest, resource ID, optional private `TraceParent`, bounded attempt count,
  maximum attempts, availability time, creation time, and update time. Public
  job models exclude the key, semantic digest, and trace metadata. All timestamps
  normalize to UTC and cannot move backwards.
- The idempotency namespace is `(job kind, idempotency key)`. A run and a
  comparison may use the same opaque key without identifying the same job.
- The semantic digest covers the validated effective request with all defaults
  materialized. It does not cover JSON member order, raw transport bytes, or the
  separately stored idempotency key. It also excludes trace context because
  correlation does not change requested work.
- An atomic submission insert has one winner. It stores the job and canonical
  resolved payload in one transaction. An identical request returns the original
  job and payload without another enqueue. Reusing the same kind and key with a
  different semantic digest is a conflict. The insert winner's validated trace
  context remains authoritative on an exact replay.
- Dataset lookup, adapter and evaluator validation, comparison alignment, and
  derived-work bounds are checked before a new job is claimed whenever they can
  be resolved without execution.
- A queued job may be claimed as `running` or canceled immediately. Running work
  may be rescheduled as `queued`, receive `cancel_requested`, or end as
  `succeeded` or `failed`. A cancellation request ends as `canceled`. The three
  terminal states never transition again, and queued work always has an attempt
  remaining.
- Only failed jobs contain an error code, and that value is a bounded stable
  code. Raw exception text, database details, and local paths never enter a job
  record.
- A claim increments the job attempt count and creates exactly one corresponding
  running attempt. Attempt numbers are positive, ordered, and never exceed the
  job maximum. Public attempt models omit worker identities and lease tokens.
- The database clock decides claim eligibility, lease expiry, heartbeat expiry,
  retry availability, cancellation time, and terminal time. Worker clocks do not
  decide ownership.
- Heartbeat, retry, failure, cancellation, and completion operations are fenced
  by the active job ID, attempt number, private token, status, and unexpired
  lease. A stale or superseded attempt cannot mutate the job or publish evidence.
- An explicitly transient failure or an expired lease is rescheduled with
  bounded backoff while another attempt remains. Exhaustion records a safe
  terminal failure. Cancellation takes precedence over retry or success.
- Successful completion inserts the immutable `RunRecord` or
  `ReleaseDecisionRecord`, completes the active attempt, and transitions its job
  to `succeeded` in one database transaction. A failed transaction publishes no
  partial state; an exact response-lost retry may confirm identical evidence.
- A job's resource kind and ID must agree with the evidence completed through
  it. A changed immutable resource at an existing identity is a conflict.
- The API only validates and enqueues. Leased workers execute immutable payloads,
  heartbeat, and publish through fenced transactions. This guarantees at most
  one durable evidence resource for a job, not exactly-once provider invocation
  or exactly-once external side effects.
- A worker treats the durable `TraceParent` as an optional W3C Link, not as a
  parent span and never as authorization. Invalid legacy or stored context is
  ignored rather than copied into telemetry.

## Persistence invariants

### Local CLI artifact store

- A run ID is validated before any path is built.
- A domain-separated hash of the run ID is used as the storage filename; the raw
  identifier is verified inside the artifact on read.
- Save is create-once. Identical retries succeed; different content for an
  existing ID conflicts; no save path overwrites an artifact.
- Stored bytes are a versioned RFC 8785 envelope plus one final LF.
- Reads accept regular files only, enforce a 64 MiB limit, validate strict JSON,
  reject unknown envelope fields, verify the embedded run ID, and require exact
  canonical bytes.

### PostgreSQL control-plane store

- Dataset, run, and release-decision records are append-only. An identical put is
  idempotent; different canonical content at the same immutable identity is a
  conflict. No operation overwrites completed evidence.
- Job creation is protected by unique identities and the kind/key idempotency
  namespace. Each nonterminal job has one bounded canonical payload whose kind,
  digest, and resource dependencies are validated when it is loaded.
- PostgreSQL workers select claimable jobs and expired attempts with row locks
  and `SKIP LOCKED`. Competing workers can make progress without sharing one job.
  Lease operations use database time and compare-and-set fencing so concurrent
  writers cannot skip or reverse lifecycle transitions.
- Attempt history is append-only per `(job_id, attempt_number)`. Terminal attempt
  metadata retains bounded timing and safe error codes while private worker and
  lease identities remain persistence-only coordination data.
- Stored domain documents retain complete canonical evaluation evidence. Public
  API models are separate redacted summaries; database records are not shaped by
  the response contract.
- List operations use a positive bounded limit and an opaque continuation cursor.
  Cursor decoding, filtering, and ordering remain repository concerns; malformed
  or out-of-range values fail without exposing database details.
- Alembic is the only schema migration path. API readiness requires database
  connectivity and the exact expected migration head.
- Database connection configuration and credentials are runtime inputs, never
  domain fields, artifact identities, semantic request input, or error evidence.

## Project authorization and telemetry invariants

- `control-plane-auth/v1` contains exactly one project and one to 512 ordered,
  uniquely identified principals. A principal contains only a bounded identity,
  a unique SHA-256 bearer digest, and a nonempty, unique, lexicographically
  ordered scope tuple. The raw credential is not a model field.
- A bearer credential has the exact `cpk_` prefix followed by 43 URL-safe
  characters. Runtime matching hashes the presented value and compares digests
  in constant time. Authentication failures do not disclose whether a principal,
  digest, or project exists.
- Every protected request contains exactly one bounded `X-Project-ID` equal to
  the configured project. One deployment and database own one project. The
  header is a fail-closed routing assertion, not a selector over shared tenant
  rows.
- `control-plane:read` permits protected reads, `control-plane:write` permits
  ordinary mutations, `control-plane:cancel` permits cancellation, and
  `observability:read` permits metrics retrieval. Missing or malformed
  authentication fails with `401`; a wrong project or missing scope fails with
  `403`.
- API telemetry uses only bounded methods, route templates, status classes,
  stable public error codes, authorization outcomes, durations, generated
  request IDs, and valid trace identifiers. Other caller-controlled values collapse to
  fixed fallback categories and never create metric-label cardinality.
- Worker telemetry uses only bounded poll outcomes, job kinds, durable results,
  recovery counts, readiness, lifecycle state, duration, and trace identifiers.
  Worker IDs, lease tokens, job IDs, and resolved payloads are not telemetry
  fields.
- Prompts, expectations, target outputs, request or response bodies, SQL, rows,
  authorization material, project and principal identity, idempotency keys,
  semantic request digests, raw cursors, database configuration, and exception
  text are excluded from logs, metrics, traces, events, and links.
- The API accepts exactly one lowercase W3C `traceparent` version `00` value.
  Invalid or duplicate values are ignored and `tracestate` is not propagated.
  HTTP spans use route templates; linked worker, run, target, and evaluator spans
  carry no evaluation content or exception events.
- Telemetry providers and registries are dependency-injected and isolated.
  Telemetry export, clock, sink, or instrumentation failure must not change API,
  worker, idempotency, recovery, or evidence behavior.

## API resource invariants

- Top-level public contracts carry a literal schema version. Breaking transport
  changes require a new version instead of silently changing an existing shape.
- Mutating bodies require strict UTF-8 `application/json` without content
  encoding. Duplicate names, BOMs, non-finite numbers, malformed or excessively
  nested input, and oversized bodies are rejected before application execution.
- One registered dataset contains at most 1,000 cases. Slice fan-out, total
  distinct slices, evaluators, comparison gates, metrics, case-comparison
  records, and aggregate scan work have explicit upper bounds.
- Collection pages contain at most 100 items. Filters accept documented exact
  dataset-name selectors and enum values for job kind, job status, or release
  status.
- Run and release-decision submission/detail contracts contain safe identifiers,
  digests, counts, aggregates, and gate results. Collection contracts use
  bounded metadata projections—resource identifiers, kind or status, safe
  failure codes, digests, timestamps, dataset identity and case count, execution
  mode, and comparison run IDs where applicable—without loading canonical
  evidence documents. All variants exclude raw inputs, expectations, target
  outputs, SQL, rows, idempotency keys, semantic request digests, database URLs,
  local paths, and exception text.
- Job summaries expose status, attempt counts, availability, safe failures, and
  timestamps. Attempt summaries expose only attempt number, status, safe failure,
  and timing; neither surface exposes the resolved payload, worker identity, or
  lease token.
- Request and application failures use a versioned safe envelope and generated
  request ID; caller request IDs are ignored. Validation details include only
  sanitized locations and stable
  error types, never rejected values, validator context, URLs, or exception
  strings. Readiness uses the separate `health/v1` status contract.

## Evaluation-specification semantics

For `higher_is_better`, the absolute gate passes when a candidate metric is at
least the configured threshold. For `lower_is_better`, it passes when the value
is no greater than the threshold.

`allowed_regression` is an absolute budget in the metric's own units. Every
delta is `candidate - baseline`. Therefore a higher-is-better regression passes
when `delta >= -allowed_regression`; a lower-is-better regression passes when
`delta <= allowed_regression`. A fixed `1e-12` absolute tolerance prevents
machine-precision noise at an exact numeric boundary and is not a user-visible
regression allowance.

When `slice` is absent, a gate covers the whole dataset. When present, it covers
exactly the cases carrying that slice label. Empty or unknown gate slices are
configuration errors rather than zero-valued aggregates. A policy cannot define
the same `(metric, slice)` gate twice.

Before gate evaluation, the comparator requires:

- candidate and baseline runs to use the exact supplied dataset artifact;
- policy target names/revisions and optional digests to match resolved runs;
- identical ordered case IDs, metric sets, evaluator revisions, and digests;
- stored global metric summaries to equal aggregates recomputed from cases.
- candidate and baseline runs to use the same execution mode.

A gate's coverage check requires both sides to have zero errors, at least one
scored case, equal scored counts, and equal skipped counts. Coverage, threshold,
and regression failures have separate stable codes and may coexist.

Case evidence is classified as newly passing, newly failing, unchanged passing,
unchanged failing, or incomparable relative to that gate's threshold. Technical
target/evaluator failures are incomparable and force the relevant coverage gate
to fail.

The release decision is failed if any gate fails. Its digest includes resolved
dataset/target/evaluator identities, both result digests, execution mode, every
aggregate, gate result, and gate-scoped case transition. Run IDs are excluded so
equivalent evidence has the same decision identity.

`EvaluationSpec.schema_version` is currently the literal value `"1"`. Breaking
changes require a new schema version and migration path; optional fields may
remain compatible when defaults preserve existing behavior.
