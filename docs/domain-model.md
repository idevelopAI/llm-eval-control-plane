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
| `JobStatus` | `queued`, `running`, `succeeded`, or `failed` lifecycle state | Implemented |
| `RunRecord` | Append-only run result plus its durable creation time | Implemented |
| `ReleaseDecisionRecord` | Append-only release decision plus stable ID and creation time | Implemented |
| `CursorPage` | Stable bounded page plus opaque continuation cursor | Implemented |
| `SqlExpectation` | Reviewed query, clarification, or refusal oracle | Implemented |
| `SqlTargetOutput` | Minimal normalized decision and generated-SQL evidence | Implemented |
| `SqlReplayResult` | Bounded normalized PostgreSQL columns and rows | Implemented |
| Suite version | Dataset, evaluators, slices, and execution settings | Deferred |

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

## Execution invariants

- A target receives `case_id` and `input` only. It never receives `expected`,
  expected refusal state, schemas, tolerances, or slice labels.
- During one evaluation-runner invocation, each case is invoked exactly once.
  Durable HTTP submission does not turn that local invariant into an
  exactly-once execution guarantee across process crashes.
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
  digest, resource ID, creation time, and update time. All timestamps normalize
  to UTC and the update time cannot precede creation.
- The idempotency namespace is `(job kind, idempotency key)`. A run and a
  comparison may use the same opaque key without identifying the same job.
- The semantic digest covers the validated effective request with all defaults
  materialized. It does not cover JSON member order, raw transport bytes, or the
  separately stored idempotency key.
- An atomic job claim has one insert winner. An identical request returns the
  stored job and performs no new execution. Reusing the same kind and key with a
  different semantic digest is a conflict.
- Dataset lookup, adapter and evaluator validation, comparison alignment, and
  derived-work bounds are checked before a new job is claimed whenever they can
  be resolved without execution.
- Legal transitions are only `queued` to `running`, then `running` to either
  `succeeded` or `failed`. Terminal jobs never transition again. An exact-state
  retry is accepted only when its safe error code also matches.
- Only failed jobs contain an error code, and that value is a bounded stable
  code. Raw exception text, database details, and local paths never enter a job
  record.
- Successful completion inserts the immutable `RunRecord` or
  `ReleaseDecisionRecord` and transitions its job to `succeeded` in one database
  transaction. A failed transaction publishes neither half.
- A job's resource kind and ID must agree with the evidence completed through
  it. A changed immutable resource at an existing identity is a conflict.
- API execution is currently synchronous. If the process stops after claiming a
  job, its durable `running` state can remain stranded. Identical replay returns
  that state and does not reinvoke execution. Phase 5 must add worker leasing and
  recovery; these invariants do not promise exactly-once execution.

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
  namespace. State changes use compare-and-set semantics so concurrent writers
  cannot skip or reverse lifecycle transitions.
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
- Request and application failures use a versioned safe envelope and bounded
  request ID. Validation details include only sanitized locations and stable
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
