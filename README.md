# LLM Eval Control Plane

[![CI](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml)
[![Release Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/release-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/release-gate.yml)
[![DataBridge Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/databridge-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/databridge-gate.yml)

A deterministic-first control plane for evaluating AI application behavior,
preserving case-level evidence, and making quality, safety, latency, and usage
changes measurable before release.

> **Status:** Phase 3 DataBridge vertical slice. In addition to deterministic
> baseline comparison and release gates, the repository now evaluates strict
> DataBridge v1.2.0 query, clarification, and refusal decisions against a bounded
> read-only PostgreSQL replay environment. Mock and live execution modes are
> explicit, independently hashed evidence.

## DataBridge PostgreSQL evaluation

The pinned DataBridge fixture contains 56 reviewed cases: 40 source query cases,
eight ambiguity cases, and eight unsafe or privacy-sensitive requests. English
and German are balanced at 28 cases each. The source cases and PostgreSQL seed
are pinned to DataBridge AI `v1.2.0` commit
`27b4a6ea96a8aec331afe758cc78dff50a1c6690`; artifact hashes are recorded in
[`examples/databridge/provenance-v1.json`](examples/databridge/provenance-v1.json).

Create an empty, disposable PostgreSQL database, seed it, and provision a
separate evaluation role with only `CONNECT`, schema `USAGE`, and table `SELECT`
permissions. The seed intentionally creates neither a role nor a credential.

```bash
psql "$DATABRIDGE_ADMIN_DSN" -v ON_ERROR_STOP=1 \
  -f examples/databridge/postgres-fixture-v1.sql

# Set this out of band to the restricted evaluation role; do not paste it into
# a command, tracked file, or shell history.
test -n "${DATABRIDGE_EVAL_DSN:-}"

uv sync --locked
uv run llm-eval databridge run examples/databridge/cases-v1.jsonl \
  --run-id databridge-mock-v1 \
  --fixture-sql examples/databridge/postgres-fixture-v1.sql \
  --expected-fixture-fingerprint \
    sha256:e40acff961cc83377391195acb15d09fa2931b1cc9b3dd01ee03fcc043a21a09 \
  --responses examples/databridge/mock-responses-v1.json \
  --target-revision 1
```

Mock mode performs no target HTTP calls. It replays strict, checked-in
DataBridge wire responses through the same normalizer as the HTTP adapter, then
executes allowed SQL against the local PostgreSQL fixture. The composite scorer
records interaction decision and clarification correctness, unsafe-query
rejection, PostgreSQL parse and read-only-policy results, execution success,
column equivalence, and ordered or unordered result-set equivalence. Latency and
usage metrics are also retained. The connected database must match the pinned
normalized fingerprint before a run, and the same fingerprint must remain after
the run.

The four-case override demonstrates query-result, clarification, and unsafe-SQL
regressions without changing the reviewed dataset:

```bash
uv run llm-eval databridge run examples/databridge/cases-v1.jsonl \
  --run-id databridge-mock-regression-v2 \
  --fixture-sql examples/databridge/postgres-fixture-v1.sql \
  --expected-fixture-fingerprint \
    sha256:e40acff961cc83377391195acb15d09fa2931b1cc9b3dd01ee03fcc043a21a09 \
  --responses examples/databridge/mock-responses-v1.json \
  --response-overrides examples/databridge/regression-overrides-v2.json \
  --target-revision 2

# Expected exit code: 1, because six release gates detect the four regressions.
uv run llm-eval compare \
  examples/databridge/release-policy-v1.json \
  examples/databridge/cases-v1.jsonl \
  --baseline-run databridge-mock-v1 \
  --candidate-run databridge-mock-regression-v2
```

The offline proof completes all 56 baseline cases without technical failures
and passes all seven release gates. The four seeded regressions are then blocked
by six gates covering overall and German decision accuracy, clarification,
unsafe-query rejection, read-only policy, and result equivalence. The dedicated
`DataBridge Offline Gate` check reproduces both outcomes with a digest-pinned
PostgreSQL 17.6 image and no DataBridge API credential.

> **Evidence boundary:** mock target responses, target latency, and token usage
> are deterministic simulations. PostgreSQL replay is real local execution, but
> the mock workflow is not evidence of a deployed model's accuracy or
> performance. Live accuracy was not run or reported for this release.

Live mode calls the DataBridge `/api/v1/query` endpoint only after two explicit
opt-ins. Both the API key and the restricted replay DSN are read from named
environment variables; their values are not accepted as CLI options.

```bash
# Set DATABRIDGE_API_KEY and DATABRIDGE_EVAL_DSN through your secret manager.
test -n "${DATABRIDGE_API_KEY:-}"
test -n "${DATABRIDGE_EVAL_DSN:-}"

uv run llm-eval databridge run examples/databridge/cases-v1.jsonl \
  --run-id databridge-live-v1 \
  --fixture-sql examples/databridge/postgres-fixture-v1.sql \
  --expected-fixture-fingerprint \
    sha256:e40acff961cc83377391195acb15d09fa2931b1cc9b3dd01ee03fcc043a21a09 \
  --live-base-url https://databridge.example \
  --allow-live \
  --confirm-synthetic-database \
  --target-name databridge/live \
  --target-revision 1
```

`--live-base-url` must be an HTTPS origin without credentials, a path, query, or
fragment. Plain HTTP is rejected unless `--allow-insecure-loopback` is supplied
for an explicit loopback development endpoint. Mock response options cannot be
combined with live mode.

## Baseline comparison and release gates

The release fixture contains 40 English and German quality/refusal cases. Run
the baseline and a deliberately regressed candidate with only deterministic
scorers:

```bash
uv sync --locked
uv run llm-eval run examples/release-gate-40.jsonl \
  --run-id baseline-v1 \
  --dataset-name release-gate/offline \
  --target-name fake/release \
  --target-revision 1 \
  --scorer exact_match --scorer refusal --scorer latency

uv run llm-eval run examples/release-gate-40.jsonl \
  --run-id candidate-v2-regression \
  --dataset-name release-gate/offline \
  --target-name fake/release \
  --target-revision 2 \
  --scenario-overrides examples/release-regression-overrides.json \
  --scorer exact_match --scorer refusal --scorer latency

uv run llm-eval compare \
  examples/release-gate-spec.json \
  examples/release-gate-40.jsonl \
  --baseline-run baseline-v1 \
  --candidate-run candidate-v2-regression \
  --format markdown
```

The final command intentionally returns `1`: broad quality remains inside its
budget while the refusal-only safety slice catches a regression.

| Gate | Baseline | Candidate | Delta | Decision |
|---|---:|---:|---:|---|
| Exact match, all 40 cases | `1.0` | `0.95` | `-0.05` | Pass |
| Exact match, `language/de` | `1.0` | `0.95` | `-0.05` | Pass |
| Refusal correctness, `safety/refusal` | `1.0` | `0.875` | `-0.125` | **Fail** |
| Simulated latency, all cases | `5.0 ms` | `5.0 ms` | `0.0 ms` | Pass |

`delta` always means `candidate - baseline`. `allowed_regression` is an
absolute budget in metric units. Every gate also requires matching scored and
skipped coverage with no execution errors, so a technical failure cannot be
mistaken for a good score.

Reports support `--format json`, `--format markdown`, and `--format junit`.
Use `--output PATH` to create a new report file; existing files are never
overwritten. Reports include artifact identities, metrics, slice names, and case
IDs, but omit case inputs, expected values, and target outputs.

## Reproducible 100-case demo

The reference workflow evaluates 100 synthetic cases without network access,
credentials, paid APIs, or model-provider dependencies:

```bash
uv sync --locked
uv run llm-eval run examples/offline-100.jsonl \
  --run-id offline-100-v1 \
  --dataset-name offline-100 \
  --dataset-revision 1
```

The committed fixture deliberately mixes exact text, uppercase transformation,
numeric tolerance, structured refusal, and JSON-schema cases. Its golden run
produces:

| Metric | Mean | Scored | Skipped | Errors |
|---|---:|---:|---:|---:|
| Exact match | `0.95` | 100 | 0 | 0 |
| JSON-schema validity | `1.0` | 10 | 90 | 0 |
| Numeric tolerance | `1.0` | 5 | 95 | 0 |
| Structured refusal correctness | `1.0` | 100 | 0 | 0 |
| Simulated latency | `5.0 ms` | 100 | 0 | 0 |

The dataset digest is
`sha256:83296a96077826f7523365b6db509e06ebe056297fcba1b4203e59f63a4852f0`.
The stable result-content digest is
`sha256:2544034c0247bd53c52b044496791d3e1b800c8153538b7db14885562cad3f58`.
Both are pinned in integration tests.

The offline clock advances by a fixed 5 ms so the run artifact is reproducible.
That latency value is synthetic and is not a performance benchmark. Usage values
are deterministic fixture estimates, not provider token counts or cost claims.

## Inspecting evidence safely

Run summaries include artifact identities, counts, aggregate metrics, and
digests. Case inputs, expected values, and target outputs are omitted:

```bash
uv run llm-eval show offline-100-v1
uv run llm-eval show offline-100-v1 --case offline-001
```

Target output is disclosed only when one case is selected explicitly:

```bash
uv run llm-eval show offline-100-v1 \
  --case offline-001 \
  --include-output
```

Complete evidence is stored under `.llm-eval/` in canonical, integrity-checked,
append-only files. That directory is ignored by Git because artifacts can contain
model inputs and outputs. On POSIX systems, the store uses owner-only directory
and file permissions.

The `run` command returns `0` when execution completes, `1` when sanitized target
or evaluator failures were persisted, and `2` for input, configuration, storage,
or integrity errors. The `compare` command returns `0` for a passing release,
`1` for a valid failed release decision, and `2` when comparison could not be
performed safely.

## Current capabilities

- RFC 8785 canonical JSON with duplicate-key, non-finite-number, and malformed
  input rejection
- Content-addressed datasets whose identity is independent of JSONL authoring
  order and dataset labels
- Deterministic exact, normalized-text, JSON-schema, numeric-tolerance, refusal,
  latency, and usage scorers
- One target invocation per case, explicit scored/skipped/error observations,
  sanitized failures, and coverage-aware aggregates
- Atomic create-once local persistence with hashed storage keys, bounded reads,
  canonical-byte validation, and digest verification
- Safe JSON CLI summaries plus opt-in per-case output disclosure
- Candidate-minus-baseline comparison with strict artifact, case, evaluator,
  digest, and stored-summary alignment
- Global and slice-aware gates with absolute thresholds, regression budgets,
  coverage enforcement, and case transition evidence
- Stable JSON, Markdown, and JUnit release reports with automation exit codes
- A credential-free GitHub release check that proves both a passing candidate
  and a blocked seeded safety regression
- A pinned 56-case English/German DataBridge dataset with separate strict mock
  responses, four deliberate regression overrides, and source provenance
- Strict DataBridge v1.2.0 mock and HTTPS targets with explicit execution modes,
  bounded responses, sanitized failures, and environment-only secret lookup
- PostgreSQL SQL parsing, allowlist policy, bounded read-only replay, reviewed
  reference validation, and interaction/safety/result-equivalence metrics
- Python 3.11–3.14 CI, strict typing, linting, branch coverage, packaging, and
  isolated wheel smoke tests

## Design principles

- Prefer deterministic evaluators before introducing model-based judges.
- Version and hash every reproducibility-relevant artifact.
- Preserve case-level evidence behind every aggregate metric.
- Count skipped and failed evaluations instead of silently dropping coverage.
- Let narrow safety and language slices block a release independently of broad
  averages.
- Keep offline CI fixtures deterministic and separate from live-provider runs.
- Never expose prompts, expected values, outputs, or private exception details in
  default CLI output or telemetry.

## Development

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --locked
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --cov=llm_eval_control_plane --cov-branch
uv build
```

The original evaluation-specification contracts remain available:

```bash
uv run llm-eval schema
uv run llm-eval validate examples/evaluation-spec.json
```

## Architecture

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [Architecture decisions](docs/adr/)

The project is a modular monolith with dependency direction
`entrypoints/adapters → application → domain`. The CLI is the composition root;
the application layer depends on target, evaluator, and repository protocols,
not concrete adapters.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting and the evaluation-data
handling policy.

## License

Licensed under the [MIT License](LICENSE).
