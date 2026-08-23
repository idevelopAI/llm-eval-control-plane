# LLM Eval Control Plane

[![CI](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml)
[![Release Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/release-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/release-gate.yml)
[![DataBridge Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/databridge-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/databridge-gate.yml)
[![Control Plane API Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/control-plane-api-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/control-plane-api-gate.yml)

A deterministic-first control plane for evaluating AI application behavior,
preserving case-level evidence, and making quality, safety, latency, and usage
changes measurable before release.

> **Status:** Phase 4 durable control-plane API. The repository now exposes the
> deterministic evaluation and comparison core through a versioned FastAPI
> service backed by PostgreSQL records, Alembic migrations, immutable evidence,
> and semantic idempotency. The DataBridge PostgreSQL evaluation remains
> available through the CLI.

## Durable HTTP control plane

The local API registers immutable dataset revisions, submits deterministic
evaluation runs, tracks durable jobs, and stores release decisions. API v1 uses
only the credential-free deterministic executor: its latency and usage evidence
are simulated and must not be presented as live-model measurements.

### Local Compose quickstart

The Compose stack mounts the database password from a gitignored secret file.
Keep the value out of `.env`, command arguments, and shell history:

```bash
(
  umask 077
  mkdir -p .secrets
  chmod 0700 .secrets
  touch .secrets/postgres-password.txt
  chmod 0600 .secrets/postgres-password.txt
  printf 'Local PostgreSQL password: '
  IFS= read -r -s CONTROL_PLANE_LOCAL_PASSWORD
  printf '\n'
  printf '%s\n' "$CONTROL_PLANE_LOCAL_PASSWORD" \
    > .secrets/postgres-password.txt
  chmod 0444 .secrets/postgres-password.txt
  unset CONTROL_PLANE_LOCAL_PASSWORD
)

docker compose up --build --detach --wait
docker compose ps
curl --fail --silent http://127.0.0.1:8000/health/ready
```

The `migrate` service applies the exact Alembic head before the API starts. The
readiness endpoint requires both database connectivity and that schema revision.
The API port is bound to loopback by default.

Register a small dataset, then submit a run with a caller-selected idempotency
key:

```bash
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --request POST http://127.0.0.1:8000/v1/datasets \
  --data-binary @- <<'JSON'
{
  "name": "demo/http",
  "revision": 1,
  "cases": [
    {
      "case_id": "echo-001",
      "input": {"scenario": "echo", "value": "hello"},
      "expected": "hello"
    }
  ]
}
JSON

curl --include --fail-with-body \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: demo-run-v1' \
  --request POST http://127.0.0.1:8000/v1/runs \
  --data-binary @- <<'JSON'
{
  "dataset_name": "demo/http",
  "dataset_revision": 1,
  "target_name": "fake/http",
  "target_revision": 1,
  "evaluators": ["exact_match", "latency"]
}
JSON
```

The submission response contains a `Location: /v1/jobs/{job_id}` header. Run
submission and detail responses contain identifiers, content digests, execution
mode, case-status counts, and aggregate metrics; decision submission and detail
responses also contain gate results. Collection pages use bounded indexed
discovery projections and do not load the canonical evidence documents. Across
resources, their fields are limited to resource identifiers, kind or status,
safe failure codes, digests, timestamps, dataset identity and case count,
execution mode, and comparison run IDs where applicable. No response returns
case inputs, expectations, target outputs, SQL, rows, idempotency keys, request
digests, database URLs, or exception text.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/live` | Process liveness |
| `GET` | `/health/ready` | Database and exact-schema readiness |
| `POST`, `GET` | `/v1/datasets` | Register or page dataset revisions |
| `GET` | `/v1/dataset-revisions/{revision}/{name:path}` | Read one slash-safe dataset summary |
| `POST`, `GET` | `/v1/runs` | Submit or page evaluation runs |
| `GET` | `/v1/runs/{run_id}` | Read one redacted run summary |
| `GET` | `/v1/jobs`, `/v1/jobs/{job_id}` | Page or inspect durable job state |
| `POST` | `/v1/comparisons` | Submit a baseline/candidate comparison |
| `GET` | `/v1/release-decisions` | Page release decisions |
| `GET` | `/v1/release-decisions/{decision_id}` | Read one redacted decision |
| `GET` | `/openapi.json`, `/docs` | Read the generated contract or local Swagger UI |

The generated API contract is committed at
[`docs/openapi-v1.json`](docs/openapi-v1.json). Regenerate or verify it with:

```bash
uv run python scripts/export_openapi.py
uv run python scripts/export_openapi.py --check
```

Run and comparison submissions require `Idempotency-Key`. The service hashes the
validated effective request with defaults materialized, not the raw JSON bytes.
The same job kind, key, and semantic request returns the existing job without a
second invocation; reusing a key for different semantics returns `409`. Evidence
insertion and the terminal job transition occur in one database transaction. A
new synchronously completed submission returns `201`, a terminal replay returns
`200`, and an existing queued or running job returns `202`.

This is an intentionally local, unauthenticated service. Do not expose it to an
untrusted network. Execution currently occurs synchronously in the API process.
A process interruption can leave a claimed job in `running`; replay returns that
durable job and does not execute it again. Automated recovery and queued workers
are not implemented in this phase, so the service does not claim exactly-once
execution.

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
- A strict FastAPI v1 surface with bounded JSON bodies, stable versioned error
  envelopes, redacted summaries, opaque keyset pagination, and generated OpenAPI
- SQLAlchemy PostgreSQL persistence for datasets, jobs, immutable run evidence,
  and immutable release decisions, with Alembic schema compatibility checks
- Atomic semantic job claims, legal queued/running/terminal transitions, and
  transactional evidence completion without duplicate replay execution
- A hardened local Compose stack with a one-shot migration service, loopback API
  binding, read-only containers, dropped capabilities, and file-mounted secrets
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
uv run mypy src tests scripts migrations
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
`entrypoints/adapters → application → domain`. The CLI and API runtime are
composition roots; the application layer depends on target, evaluator, and
control-plane repository protocols, not concrete adapters.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting and the evaluation-data
handling policy.

## License

Licensed under the [MIT License](LICENSE).
