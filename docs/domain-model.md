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
| `EvaluationSpec` | Dataset, candidate, optional baseline, and gate inputs | Implemented contract |
| `MetricGate` | Directional threshold and permitted regression | Implemented contract |
| Suite version | Dataset, evaluators, slices, and execution settings | Deferred |
| Baseline comparison | Candidate and baseline metric deltas | Deferred |
| Gate decision | Deterministic pass/fail evidence | Deferred |

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
- Each case is invoked exactly once by the Phase 1 runner.
- Target responses require structured refusal state and explicit non-negative
  input/output usage. Refusals are never inferred from wording.
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
  low metric values. Release policy has not been applied yet.
- The result digest covers resolved artifacts, target evidence, observations,
  failures, and aggregates. It excludes only the caller-selected run ID.
- Loading a stored result recalculates and verifies its result digest.

## Persistence invariants

- A run ID is validated before any path is built.
- A domain-separated hash of the run ID is used as the storage filename; the raw
  identifier is verified inside the artifact on read.
- Save is create-once. Identical retries succeed; different content for an
  existing ID conflicts; no save path overwrites an artifact.
- Stored bytes are a versioned RFC 8785 envelope plus one final LF.
- Reads accept regular files only, enforce a 64 MiB limit, validate strict JSON,
  reject unknown envelope fields, verify the embedded run ID, and require exact
  canonical bytes.

## Evaluation-specification semantics

For `higher_is_better`, the absolute gate passes when a candidate metric is at
least the configured threshold. For `lower_is_better`, it passes when the value
is no greater than the threshold.

`allowed_regression` is part of the validated contract, but comparison execution
is deferred. Its absolute-versus-relative semantics will be versioned before a
gate decision is produced.

`EvaluationSpec.schema_version` is currently the literal value `"1"`. Breaking
changes require a new schema version and migration path; optional fields may
remain compatible when defaults preserve existing behavior.
