# Domain Model

## Vocabulary

| Concept | Meaning | Status |
|---|---|---|
| `ArtifactRef` | Immutable `(kind, name, revision, digest)` identity | Implemented |
| `EvaluationSpec` | Dataset, candidate, optional baseline, and gate inputs | Implemented |
| `MetricGate` | Directional threshold and permitted regression | Implemented |
| Dataset version | Immutable collection of reviewed evaluation cases | Planned |
| Target version | Immutable endpoint/model/application configuration | Planned |
| Evaluator version | Metric implementation plus resolved configuration | Planned |
| Suite version | Dataset, evaluators, slices, and execution settings | Planned |
| Evaluation run | Resolved snapshot and lifecycle of one execution | Planned |
| Case result | Append-only output, evidence, timing, usage, and error | Planned |
| Metric observation | Versioned evaluator output for one case | Planned |
| Aggregate metric | Run-level metric with sample count and slice | Planned |
| Gate decision | Deterministic pass/fail result with supporting evidence | Planned |

## Invariants

- Versions are immutable. Editing an artifact creates a new revision.
- A digest, when present, uses canonical `sha256:<lowercase hex>` form.
- An evaluation specification references exactly one dataset and candidate.
- A baseline is optional but cannot equal the candidate artifact revision.
- Dataset fields accept only dataset references; candidate and baseline fields
  accept only target references.
- Gate metric names are unique inside one specification.
- Gate thresholds are finite; regression budgets are finite and non-negative.
- Unknown fields are rejected instead of silently discarded.
- Secrets never belong to serialized domain models.

## Metric semantics

For `higher_is_better`, the absolute gate passes when a candidate metric is at
least the configured threshold. For `lower_is_better`, it passes when the value
is no greater than the threshold.

`allowed_regression` will be applied during baseline comparison. Its exact
absolute-versus-relative semantics will be made explicit in the versioned gate
policy before comparison execution is implemented.

## Schema compatibility

`EvaluationSpec.schema_version` currently has the literal value `"1"`. Breaking
changes require a new schema version and a migration path; adding optional fields
may remain compatible when their defaults preserve existing behavior.
