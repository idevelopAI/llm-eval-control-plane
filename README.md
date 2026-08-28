# LLM Eval Control Plane

[![CI](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml)
[![Release Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/release-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/release-gate.yml)
[![DataBridge Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/databridge-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/databridge-gate.yml)
[![Control Plane API Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/control-plane-api-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/control-plane-api-gate.yml)
[![Worker Recovery Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/worker-recovery-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/worker-recovery-gate.yml)
[![Security Gate](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/security-gate.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/security-gate.yml)

A deterministic-first control plane for evaluating AI application behavior,
preserving case-level evidence, and making quality, safety, latency, and usage
changes measurable before release.

> **Status:** Phase 7 privacy-bounded release analytics and a local live review
> dashboard. The FastAPI service exposes redacted case transitions and fixed
> aggregate distributions for immutable decisions. The React dashboard validates
> every response at runtime, keeps read-only credentials in volatile memory, and
> permits bearer entry only on an HTTP loopback origin. Project authorization,
> privacy-safe observability, fenced PostgreSQL recovery, and the DataBridge
> evaluation remain available.

## Release evidence dashboard

The dashboard opens in an immutable, zero-request fixture mode. When served on
loopback, an operator can explicitly connect it to the local control plane and
review the newest decision history, failed-first gates, transition-filtered case
scores, and privacy-bounded score, latency, and usage-unit distributions.

Raw evaluation content is outside the dashboard contract. Case reads expose
only IDs, slice labels, score status, pass state, numeric score, delta, and change
class. Operational quantiles are withheld below the minimum aggregate size.
Credentials never enter tracked configuration or browser persistence, and a
hosted origin cannot render the local bearer-entry form.

See the [dashboard operator guide](dashboard/README.md) for its trust boundary,
local workflow, and validation commands. Hosted live access is deliberately
deferred until a server-side session boundary is implemented.

## Durable HTTP control plane

The local API registers immutable dataset revisions, submits deterministic
evaluation runs, tracks durable jobs and attempts, accepts cancellation
requests, and stores release decisions. API v1 uses only the credential-free
deterministic executor: its latency and usage evidence are simulated and must
not be presented as live-model measurements.

### Local Compose quickstart

The Compose stack mounts the database password and authentication configuration
from gitignored files. Keep credential values out of `.env`, command arguments,
shell history, and Git:

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
  unset CONTROL_PLANE_LOCAL_PASSWORD
)
```

Create a bearer credential in a secret manager using the exact `cpk_` prefix
followed by 43 URL-safe characters. Keep that raw value outside the repository.
The authentication file stores only its SHA-256 digest and represents exactly
one project. This schematic is deliberately invalid and must not be used as a
credential or copied unchanged:

```json
{
  "schema_version": "control-plane-auth/v1",
  "project_id": "<single-deployment-project-id>",
  "principals": [
    {
      "principal_id": "<operator-id>",
      "token_digest": "sha256:<64-lowercase-hex-characters>",
      "scopes": [
        "control-plane:cancel",
        "control-plane:read",
        "control-plane:write",
        "observability:read"
      ]
    }
  ]
}
```

Write the resolved document to `.secrets/control-plane-auth.json` through a
protected local process, then make the bind-mounted files readable by the fixed
non-root container UID:

```bash
chmod 0444 \
  .secrets/control-plane-auth.json \
  .secrets/postgres-password.txt

docker compose up --build --detach --wait
docker compose ps
curl --fail --silent http://127.0.0.1:8000/health/ready
```

The `migrate` service applies the exact Alembic head before the API starts. The
API and worker start only after migration succeeds. The readiness endpoint
requires both database connectivity and that schema revision. The API port is
bound to loopback by default; the worker has no host port. To exercise competing
claims locally, scale only the worker service:

```bash
docker compose up --build --detach --wait --scale worker=2
```

Provision a mode-`0600` curl configuration outside Git from the secret manager.
It must supply the `Authorization: Bearer ...` and matching `X-Project-ID: ...`
headers. Point `CONTROL_PLANE_CURL_CONFIG` at that file; the path is not secret,
and the raw credential stays out of command arguments. Register a small dataset,
then submit a run with a caller-selected idempotency key:

```bash
test -r "${CONTROL_PLANE_CURL_CONFIG:?}"

curl --fail-with-body \
  --config "${CONTROL_PLANE_CURL_CONFIG:?}" \
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
  --config "${CONTROL_PLANE_CURL_CONFIG:?}" \
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
discovery projections and do not load the canonical evidence documents.
Resource collection fields are limited to identifiers, kind or status, safe
failure codes, digests, timestamps, dataset identity and case count, execution
mode, and comparison run IDs where applicable. Dashboard analytical routes
separately expose the score-only case and fixed aggregate fields described
above. No response returns case inputs, expectations, target outputs, SQL, rows,
idempotency keys, request digests, database URLs, raw operational samples, or
exception text.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/live` | Process liveness |
| `GET` | `/health/ready` | Database and exact-schema readiness |
| `GET` | `/metrics` | Authenticated API Prometheus metrics |
| `POST`, `GET` | `/v1/datasets` | Register or page dataset revisions |
| `GET` | `/v1/dataset-revisions/{revision}/{name:path}` | Read one slash-safe dataset summary |
| `POST`, `GET` | `/v1/runs` | Submit or page evaluation runs |
| `GET` | `/v1/runs/{run_id}` | Read one redacted run summary |
| `GET` | `/v1/jobs`, `/v1/jobs/{job_id}` | Page or inspect durable job state |
| `GET` | `/v1/jobs/{job_id}/attempts` | Inspect redacted attempt history |
| `POST` | `/v1/jobs/{job_id}/cancellation` | Cancel queued work or request running cancellation |
| `POST` | `/v1/comparisons` | Submit a baseline/candidate comparison |
| `GET` | `/v1/release-decisions` | Page release decisions |
| `GET` | `/v1/release-decisions/{decision_id}` | Read one redacted decision |
| `GET` | `/v1/release-decisions/{decision_id}/cases` | Page score-only decision cases for one gate |
| `GET` | `/v1/release-decisions/{decision_id}/distributions` | Read fixed score and operational distributions |
| `GET` | `/openapi.json` | Read the generated API contract |

The runtime does not serve an interactive documentation UI, so a
credential-handling browser page never loads third-party documentation assets.
The generated API contract is committed at
[`docs/openapi-v1.json`](docs/openapi-v1.json). Regenerate or verify it with:

```bash
uv run python scripts/export_openapi.py
uv run python scripts/export_openapi.py --check
```

Run and comparison submissions require `Idempotency-Key`. The service hashes the
validated effective request with defaults materialized, not the raw JSON bytes.
The same job kind, key, and semantic request returns the existing job without a
second enqueue; reusing a key for different semantics returns `409`. A new or
nonterminal submission returns `202`; a replay of a terminal job returns `200`.
Both responses carry the job `Location` header. Submission handlers never invoke
the target, an evaluator, or the comparison engine.

Every `/v1` request is authenticated and project-bound. Reads require
`control-plane:read`, mutations require `control-plane:write`, cancellation
requires `control-plane:cancel`, and `/metrics` requires `observability:read`.
The exact `X-Project-ID` is a fail-closed routing assertion: one deployment and
database own one project, and the service does not claim row-level
multitenancy. Compose remains loopback-only because TLS termination and
distributed rate limiting are external responsibilities.

Jobs progress through `queued`, `running`, `cancel_requested`, `succeeded`,
`failed`, or `canceled`. Each claim creates a redacted attempt record and a
private expiring lease. Workers heartbeat active leases; the reaper either
reschedules an expired attempt with bounded backoff or fails it after the
configured attempt limit. Queued cancellation is immediate, while running
cancellation is cooperative and wins any later publication race.

Provider or target invocation is at least once: a worker can lose its lease
after an external call and another worker may retry it. Fencing provides
exactly-once durable evidence publication for a job, not exactly-once external
side effects. Attempt lease tokens, worker identities, idempotency keys, semantic
request digests, and resolved payloads are never returned by the API.

### Observability and trace continuity

The API emits one fixed-schema `control-plane-log/v1` JSON completion event per
request. Logs, metrics, and traces use route templates and bounded vocabularies;
they exclude bodies, prompts, expectations, outputs, SQL, rows, authorization
material, project and principal identity, idempotency keys, request digests,
lease data, raw cursors, and exception text.

The authenticated `/metrics` endpoint exposes only the API instance registry:

- `control_plane_http_requests_total`
- `control_plane_http_request_duration_seconds`
- `control_plane_http_errors_total`
- `control_plane_http_requests_in_progress`
- `control_plane_auth_decisions_total`
- `control_plane_job_queue_depth`
- `control_plane_failed_jobs`
- `control_plane_evaluation_usage_units`
- `control_plane_operational_snapshot_ready`

The last four instruments come from one fixed aggregate PostgreSQL query and
never load evidence documents.

Workers maintain separate low-cardinality poll, job-duration, result, recovery,
and readiness instruments in their isolated process registry and emit safe JSON
lifecycle events. The current Compose worker has no HTTP port, so those worker
metrics are not published through a scrape endpoint.

The API accepts exactly one strict lowercase W3C `traceparent` version `00`
header. Invalid, duplicate, or differently cased values are ignored, and
`tracestate` is not propagated. A generated or accepted trace context is stored
as private job coordination metadata. The asynchronous worker starts a new
consumer span with one W3C Link to the submission span, then creates content-free
run, target, and evaluator spans below it. Trace context is not authorization,
does not affect semantic idempotency, and never permits private evaluation
content in telemetry. Completed spans are exported as fixed-schema
`trace.span.completed` JSON events that omit every span attribute and event; no
external OTLP collector is configured by default.

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
- Enqueue-only semantic submissions with immutable resolved payloads, six-state
  job lifecycles, redacted attempt history, and cooperative cancellation
- PostgreSQL worker claims using database time and `FOR UPDATE SKIP LOCKED`,
  expiring leases, heartbeats, bounded retry backoff, and expired-lease recovery
- Fenced transactional completion that publishes immutable evidence at most once
  while explicitly preserving at-least-once target invocation semantics
- Digest-only bearer authentication with exact project assertion and separate
  read, write, cancellation, and observability scopes for one project per
  deployment and database
- A responsive release-evidence dashboard with explicit fixture/live modes,
  volatile read-only credentials, recent-decision navigation, failed-first gate
  review, transition filtering, bounded case pagination, and accessible states
- Strict runtime response allowlists and cross-response integrity checks for
  decision identity, gate aggregates, case arithmetic, distribution counts, and
  stale or contradictory evidence
- Privacy-safe structured logs, low-cardinality Prometheus metrics, strict W3C
  request context, durable submission trace links, and content-free worker,
  target, and evaluator spans
- A hardened local Compose stack with one-shot migration, scalable portless
  workers, loopback API binding, read-only containers, dropped capabilities, and
  file-mounted secrets
- Full-history secret scanning, dependency and static security analysis,
  container vulnerability and configuration gates, CodeQL, and weekly locked
  dependency updates
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
- [Threat model](docs/security/threat-model.md)
- [Incident and recovery runbook](docs/operations/recovery.md)

The project is a modular monolith with dependency direction
`entrypoints/adapters → application → domain`. The CLI and API runtime are
composition roots; the application layer depends on target, evaluator, and
control-plane repository protocols, not concrete adapters.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting, authentication,
telemetry, supply-chain, recovery, and evaluation-data handling policy.

## License

Licensed under the [MIT License](LICENSE).
