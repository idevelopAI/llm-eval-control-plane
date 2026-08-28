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

The FastAPI service authenticates bearer credentials and authorizes protected
operations inside one deployment. A credential has the exact `cpk_` prefix
followed by 43 URL-safe characters. The strict `control-plane-auth/v1`
configuration stores only a `sha256:<64 lowercase hexadecimal characters>`
digest, never the raw bearer value. The file must be an absolute, bounded,
regular, non-symlink file and is required at API startup.

Every protected request also supplies exactly one `X-Project-ID` matching the
configured project. Authorization is split into these scopes:

- `control-plane:read` for `GET`, `HEAD`, and `OPTIONS` under `/v1`;
- `control-plane:write` for `/v1` mutations other than cancellation;
- `control-plane:cancel` for the cancellation operation; and
- `observability:read` for `/metrics`.

One deployment and one database own one project. `X-Project-ID` is a fail-closed
routing assertion, not row-level multitenancy. Never configure multiple projects
in one database or describe this boundary as tenant isolation. Invalid or
missing authentication returns a content-safe `401` with a Bearer challenge;
wrong project or insufficient scope returns a content-safe `403`. Neither path
echoes a credential, digest, project, or principal.

Compose binds the API to `127.0.0.1` by default. The application does not
terminate TLS or enforce distributed rate limits. Any non-loopback deployment
requires a maintained TLS-terminating gateway, request normalization, rate
controls, and one isolated control-plane deployment per project.

API request handling is intentionally fail-closed:

- mutating bodies must use `application/json` without content encoding;
- strict parsing rejects invalid UTF-8, duplicate keys, BOMs, non-finite values,
  malformed or excessively nested JSON, and bodies over the configured limit;
- dataset size, slice fan-out, evaluator count, comparison gates, and derived
  comparison work are bounded before a job is claimed;
- caller request IDs are ignored; the boundary generates a fresh internal
  correlation ID and replaces any downstream response header;
- validation details contain safe field locations and error types, never rejected
  values, validator context, URLs, or raw exceptions; and
- request and application errors use the versioned `api-error/v1` envelope and a
  generated request ID. Readiness is the deliberate exception: a non-ready
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

## Dashboard trust boundary

The release dashboard is a content-free review client, not an evidence export.
Its default fixture mode is deterministic and performs no request. Browser
bearer entry is rendered only when the dashboard is served over plain HTTP on a
loopback hostname. The Vite development proxy independently requires an
explicit loopback HTTP origin with a port. Hosted builds must keep live mode
disabled until a server-side session or backend-for-frontend boundary is
implemented.

Use only a `control-plane:read` credential. The raw bearer value is retained in
one component-scoped closure and is never written to React state, local storage,
session storage, cookies, URLs, logs, tracked environment files, or error text.
The form resets after capture. Disconnect, unmount, and `401`/`403` responses
abort active reads and drop the retained reference. A browser compromise while a
live session is connected can still access that credential; the local-only
origin restriction reduces exposure but is not a sandbox or substitute for
short-lived, least-privilege credentials.

The API returns `Cache-Control: no-store` on every response. The client also
requests `no-store`, rejects redirects, suppresses referrers, and sends the
credential only to its same origin. Runtime allowlists reject unknown response
fields, malformed version identifiers, invalid counts, unordered quantiles, and
unsafe request IDs. The view model additionally reconciles list/detail identity,
decision and gate outcomes, aggregate membership, case arithmetic and change
classes, distribution/run identity, and count relationships. Superseded reads
are aborted and guarded by a generation counter so late completions cannot
replace current evidence. Non-authorization failures are isolated to the case or
distribution panel, and retry only that projection while retaining the validated
sibling. Authorization failures are never isolated: they abort sibling reads,
discard prior evidence, and clear the volatile session.

Case projections contain only bounded IDs, slice labels, score status, numeric
score, pass state, candidate-minus-baseline delta, and transition class. They do
not contain prompts, expected values, target outputs, SQL, rows, provider
responses, failure text, or exception details. Operational responses contain
only fixed aggregate statistics and counts; quantiles below 20 measurements are
suppressed. The browser retains at most 500 paged cases for one selected gate.

Decision IDs, case IDs, slice labels, metric names, timestamps, digests, and
aggregate values remain observable to an authorized reader and may themselves
be sensitive. Do not use the dashboard on shared or untrusted machines, include
it in public screenshots with private identifiers, or treat redaction as
anonymization.

## Telemetry boundary

The API and worker use isolated Prometheus registries and isolated OpenTelemetry
tracer providers; they do not mutate global providers. API request metrics use
only bounded methods, route templates, status classes, and stable error or
authorization outcomes. It also exports fixed-cardinality persisted queue depth,
failed-job count, and aggregate input/output usage through one aggregate query.
The authenticated `/metrics` route publishes only the API process registry.
Workers maintain separate poll, job-duration, result, recovery, and readiness
instruments, but the current worker runtime has no HTTP scrape listener.

Structured events use the fixed `control-plane-log/v1` schema. Request events
contain a generated request ID, trace and span IDs, route template, method, status,
duration, outcome, and an optional stable error code. Worker events contain
only lifecycle state, bounded recovered-job count, safe job kind and outcome,
duration, and trace identifiers. Uvicorn access logging is disabled in the
production API runtime so a raw request target cannot bypass this schema.

The request boundary accepts at most one strict lowercase W3C `traceparent`
version `00` value. Duplicate or invalid values are ignored; `tracestate` is not
propagated. The active trace context is stored as private durable job metadata,
excluded from semantic request identity, and never returned by the API. A worker
starts a new consumer span with at most one W3C Link to the submission span,
rather than making an asynchronous job a child of the HTTP request. Deterministic
run, target, and evaluator spans carry no evaluation content.
The default exporter emits only fixed `trace.span.completed` JSON envelopes with
allowlisted operation, kind, outcome, timing, and linkage IDs; it discards all
span attributes and events. No external OTLP destination is enabled by default.

Telemetry is metadata, never an evidence export. Prompts, expectations, target
outputs, request and response bodies, SQL, rows, authorization material, project
or principal identity, idempotency keys, semantic request digests, database
configuration, worker identities, lease tokens, raw cursors, and exception text
are prohibited from log fields, metric labels, span attributes, events, and
links. Telemetry failures are isolated from request and worker correctness.

## Local authentication and PostgreSQL secret handling

The authentication configuration path is selected by
`CONTROL_PLANE_AUTH_CONFIG_FILE` on the Compose host and mounted as
`CONTROL_PLANE_AUTH_FILE` inside the API container. The tracked environment
example contains only that path. Generate raw bearer values through a secret
manager, write only their SHA-256 digests into the mounted JSON document, grant
each principal the minimum sorted scope set, and keep both the raw values and
resolved configuration out of Git. Recreate the API process after rotation;
configuration is loaded once and validated fail-closed at startup.

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
values. Direct database URLs reject all query options because drivers may render
query values without password masking. Migration and application errors must
never render a database URL or password.

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

## Supply-chain and recovery controls

The dedicated security workflow runs five required checks:

- `Dependency Vulnerability Audit` installs the locked security environment and
  audits the resolved local packages;
- `Static Security Analysis` enforces Ruff security rules and deployment
  hardening contracts;
- `Secret History Scan` scans every reachable commit with a checksum-verified,
  fully redacted Gitleaks binary;
- `Container Security Gate` rejects critical runtime vulnerabilities, fixable
  high runtime vulnerabilities, and high-risk deployment misconfiguration; and
- `CodeQL Python` runs the `security-extended` query suite with only the
  permissions needed to read source and upload code-scanning results.

Executable Actions are pinned to full commit SHAs, external container bases are
manifest-digest pinned, scanner jobs receive no deployment secrets, and weekly
Dependabot updates cover uv, GitHub Actions, and Docker. Pin updates still
require source and transitive-action review; a passing scanner does not prove an
artifact is benign.

The implementation-specific [threat model](docs/security/threat-model.md)
defines assets, trust boundaries, telemetry minimization, supply-chain threats,
and residual risk. The [incident and recovery runbook](docs/operations/recovery.md)
covers containment, credential rotation, isolated PostgreSQL restoration,
migration failure, worker crash recovery, and safe return to service. This
repository does not schedule, encrypt, replicate, or retention-manage backups
and makes no recovery point or recovery time objective claim.
