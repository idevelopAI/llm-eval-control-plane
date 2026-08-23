# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting feature for this repository.

Include the affected version, reproduction steps, impact, and any suggested
mitigation. Avoid attaching real credentials, private prompts, customer
documents, database rows, or other sensitive evaluation inputs.

## Data-handling baseline

Evaluation payloads are potentially sensitive. The project follows these rules:

- Credentials are referenced by secret identifiers and are never embedded in
  artifact versions or run specifications.
- Prompts, responses, SQL, rows, documents, and tool arguments are excluded from
  logs, metrics, and traces by default.
- Target outputs are treated as untrusted input and must be validated before
  scoring or display.
- CLI summaries omit case inputs, expected values, target outputs, and raw
  exception text. Output disclosure requires both a case selection and the
  explicit `--include-output` flag.
- Complete run artifacts can contain evaluation content. `.llm-eval/` is ignored
  by Git; POSIX local stores use owner-only `0700` directories and `0600` files.
- Stored runs are create-once, size-bounded, canonicalized, and integrity-checked
  when read. Run identifiers are hashed before use as filenames.
- CI uses deterministic public or synthetic fixtures and does not require paid
  model access.
- Release reports expose bounded artifact identities, aggregate values, slice
  labels, case IDs, and failure codes. They omit inputs, expectations, target
  outputs, exception text, and absolute artifact-store paths.
- `compare --output` creates a new report and refuses to overwrite an existing
  path. Treat reports as internal evidence when case IDs or metric topology are
  sensitive.
- Live evaluation is an explicit mode with separate configuration and evidence
  retention controls.

## Control-plane API trust boundary

The FastAPI service is a local development control plane. It has no user
authentication, authorization, tenant isolation, or TLS termination. Compose
binds it to `127.0.0.1` by default. Do not change that binding or place the
service behind a public endpoint without adding an authenticated gateway,
transport security, request-rate controls, and an explicit tenant model.

API request handling is intentionally fail-closed:

- mutating bodies must use `application/json` without content encoding;
- strict parsing rejects invalid UTF-8, duplicate keys, BOMs, non-finite values,
  malformed or excessively nested JSON, and bodies over the configured limit;
- dataset size, slice fan-out, evaluator count, comparison gates, and derived
  comparison work are bounded before a job is claimed;
- caller request IDs are accepted only through a bounded safe alphabet;
- validation details contain safe field locations and error types, never rejected
  values, validator context, URLs, or raw exceptions; and
- request and application errors use the versioned `api-error/v1` envelope and a
  sanitized request ID. Readiness is the deliberate exception: a non-ready
  service returns `503` with the versioned `health/v1` status contract.

Run and comparison responses are summaries. They exclude case inputs,
expectations, target outputs, SQL, rows, database configuration, idempotency
keys, semantic request digests, and exception text. Complete run and release
evidence remains in PostgreSQL and must be handled as sensitive evaluation data.
Content digests prove integrity; they do not encrypt the stored document.

Job and attempt responses additionally exclude resolved worker payloads, worker
identities, and lease tokens. Those values are private database coordination
data and must not enter logs, health files, metrics, traces, errors, or command
arguments. Canonical worker payloads can contain prompts, expectations, policy,
and other evaluation inputs; protect them like complete evidence.

`Idempotency-Key` is stored to coordinate retries. Treat it as an opaque routing
identifier, not a place for credentials, emails, prompts, or customer data. The
validated request semantics are hashed separately. An identical retry returns
the existing durable job, while a changed request using the same key fails with
a conflict.

The API process validates and durably enqueues work but never invokes a target,
evaluator, or comparison. A separate internal worker uses private expiring leases
and heartbeats. Every retry, cancellation, failure, and evidence publication is
fenced against the active attempt in PostgreSQL. The reaper reschedules expired
leases within a bounded attempt budget and fails exhausted work with a safe code.

This recovery contract is at least once for target or provider invocation. A
worker can complete an external call and crash before fenced publication, so a
later attempt may repeat that call. The database transaction prevents stale
workers from publishing duplicate or changed evidence, but it cannot roll back
external effects. Cancellation is cooperative and likewise cannot undo an
effect that occurred before the worker observed the request. Real providers must
use their own idempotency or deduplication controls when duplicate effects are
unsafe.

## Local PostgreSQL secret handling

The Compose stack reads the PostgreSQL password from the gitignored file named
by `CONTROL_PLANE_PASSWORD_FILE`. `.env.example` contains only non-secret
settings and the secret-file path. Never place the password or a complete
database URL in `.env`, Compose YAML, Git, issue text, command arguments, or
shell history.

Protect the containing directory from other host users, then make the file
readable by the fixed non-root container UID. The directory's `0700` mode blocks
host traversal even though the directly bind-mounted file uses mode `0444`:

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
```

Compose mounts that file read-only under `/run/secrets/` for PostgreSQL, the
migration process, the API, and workers. Runtime configuration reads only a bounded
regular file without following symlinks. SQLAlchemy engines hide parameter
values. Migration and application errors must never render a database URL or
password.

Use a dedicated local control-plane database and role. API and worker runtimes do
not create or upgrade tables: the one-shot Alembic service applies migrations
before startup, and readiness fails unless the database reports the exact
expected revision. The worker is attached only to the internal Compose network
and publishes no host port. Backups of the named PostgreSQL volume contain
sensitive payloads and evidence and must receive the same protection as the
source evaluation data.

## DataBridge trust boundaries

The checked-in DataBridge workflow uses deterministic mock target responses and
does not make target network requests. SQL is still replayed against a
disposable PostgreSQL fixture through the restricted DSN named by
`DATABRIDGE_EVAL_DSN`. The seed file contains only schema and synthetic rows; it
does not create a login role or contain a password.

Treat the following controls as cumulative rather than interchangeable:

- Every candidate and reference statement is parsed with the PostgreSQL dialect
  and must be exactly one query. Comments, write/DDL nodes, system schemas,
  unlisted tables, and unlisted or side-effecting functions are rejected before
  replay.
- Each accepted statement uses a fresh connection and explicit
  `BEGIN TRANSACTION READ ONLY`, with local statement and lock timeouts, UTC,
  a fixed `public` search path, bounded rows, columns, cells, and encoded result
  bytes, followed by rollback.
- The DSN should identify a separately provisioned least-privilege role with
  only the connection, schema-usage, and table-select rights needed by the
  synthetic fixture. Do not use an owner or migration role for evaluation.
- SQL policy or database failures are converted to stable codes. Raw SQL,
  database exception text, DSNs, and server details are not copied into failure
  messages.
- The fixture identity covers both the reviewed seed-file digest and a pinned
  normalized database-content fingerprint. The CLI verifies that fingerprint
  before and after evaluation. Use an empty, disposable database initialized
  from that exact file; do not point evaluation at production or customer data.

Live DataBridge calls require `--live-base-url`, `--allow-live`, and
`--confirm-synthetic-database`. The API credential is resolved from the
environment variable named by `--api-key-env` (default
`DATABRIDGE_API_KEY`), while PostgreSQL replay uses the variable named by
`--database-dsn-env` (default `DATABRIDGE_EVAL_DSN`). Only these environment
variable names—not their values—may enter configuration identities. Never put a
secret in the base URL, dataset, response fixture, run ID, artifact name, or
command-line option. The API-key and database-DSN environment references must be
different, preventing a DSN from being transmitted as an API credential.

The HTTP adapter requires HTTPS, except for an explicit loopback-only developer
override. It disables redirects and proxy-environment inheritance, verifies TLS,
caps timeouts at 60 seconds and response bodies at 256 KiB, and strictly
validates UTF-8 JSON and the DataBridge v1.2.0 response shape. A `403` becomes a
structured policy refusal without retaining its response body. Authentication,
rate-limit, timeout, transport, rejection, and protocol failures use sanitized
typed codes.

Before persistence, the DataBridge adapter removes natural-language answers,
returned rows and columns, request IDs, and provider timings. It retains the
normalized decision, generated SQL for query decisions, and usage counters.
Because generated SQL may itself contain sensitive identifiers or literals,
the complete `.llm-eval/` store remains sensitive and must not be committed.

Mock evidence is deterministic and simulated. It is suitable for control-plane
and release-gate verification, not for claims about deployed-model accuracy,
latency, token use, or cost. Live accuracy was not run for this release.
