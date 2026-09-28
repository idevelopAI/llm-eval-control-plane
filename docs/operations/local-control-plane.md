# Local control-plane operations

Run these commands from the repository root. This guide requires Docker Compose
for the local API, worker, and PostgreSQL stack. The [offline suite
workflow](../suite-cli.md) does not require Docker or a database.

For browser controls, see the [dashboard operator guide](../../dashboard/README.md).

The local API registers immutable dataset revisions, submits deterministic
evaluation runs, tracks durable jobs and attempts, accepts cancellation
requests, and stores release decisions. API v1 uses only the credential-free
deterministic executor: its latency and usage evidence are simulated and must
not be presented as live-model measurements.

The application core also registers immutable evaluation suites and submits
suite-backed runs and comparisons. Each job pins the complete suite snapshot;
workers verify the executor contract and use that snapshot without reloading a
suite alias. Run and decision digests bind the exact suite identity, and a
comparison applies only that suite's policy. Historical suite-unpinned evidence
keeps its original serialization and digests.

The [offline suite CLI](../suite-cli.md) builds and validates a resolved
protocol, runs baseline and candidate targets, and compares their pinned
evidence without Docker, hosting, or provider API calls.

The local API exposes authenticated suite registration, revision lookup,
suite-backed run/comparison submission, and newest-first experiment history for
an exact suite pin. History pages use indexed metadata without loading case
documents. Run and decision detail responses include
the resolved `suite` reference when pinned; historical unpinned responses remain
unchanged. See the [evaluation-suite API guide](../evaluation-suites.md) for
the exact inputs, permissions, replay behavior, and compatibility boundary.
Browsing this metadata does not create runs or contact a provider. Separate,
explicitly authorized local dashboard forms can [submit an offline suite
run](../../dashboard/README.md#run-the-smallest-offline-suite) or [compare selected
runs](../../dashboard/README.md#create-a-comparison-locally). Each submission uses
a one-request write credential; the read-only session is not upgraded. Legacy
run/comparison endpoints and CLI commands remain unpinned and cannot replace
suite policy.

## Local Compose quickstart

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

The stack uses PostgreSQL 18's parent-directory volume layout. A named volume
created by PostgreSQL 17 must not be attached to the PostgreSQL 18 service and
started in place. Preserve and verify a backup, then use `pg_upgrade` or a
logical dump/restore into a fresh PostgreSQL 18 volume as described in the
[recovery runbook](recovery.md#postgresql-major-version-upgrade).

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
mode, comparison run IDs, and resolved suite/target references where applicable.
Dashboard analytical routes
separately expose the score-only case and fixed aggregate fields described in the [dashboard operator guide](../../dashboard/README.md). No response returns case inputs, expectations, target outputs, SQL, rows,
idempotency keys, request digests, database URLs, raw operational samples, or
exception text.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/live` | Process liveness |
| `GET` | `/health/ready` | Database and exact-schema readiness |
| `GET` | `/metrics` | Authenticated API Prometheus metrics |
| `POST`, `GET` | `/v1/datasets` | Register or page dataset revisions |
| `GET` | `/v1/dataset-revisions/{revision}/{name:path}` | Read one slash-safe dataset summary |
| `POST`, `GET` | `/v1/suites` | Register or page immutable evaluation suites |
| `GET` | `/v1/suite-revisions/{revision}/{name:path}` | Read one suite protocol summary |
| `POST`, `GET` | `/v1/suite-runs` | Submit a pinned run or page exact-suite run history |
| `POST`, `GET` | `/v1/suite-comparisons` | Submit a pinned comparison or page exact-suite release history |
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
[`docs/openapi-v1.json`](../openapi-v1.json). Regenerate or verify it with:

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

## Observability and trace continuity

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
