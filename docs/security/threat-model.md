# Threat model

## Scope and security objective

This document covers the FastAPI control plane, leased workers, PostgreSQL
repository, deterministic evaluation paths, DataBridge adapter, local artifact
store, Compose deployment, and continuous-integration supply chain.

The security objective is to preserve the confidentiality and integrity of
evaluation inputs and evidence while ensuring that only authenticated,
authorized callers can enqueue or inspect work. Availability controls are
bounded rather than absolute: the service must reject oversized or malformed
work predictably, but it does not claim resistance to a sustained distributed
denial-of-service attack.

The supported authorization boundary is **one deployment for one project**. A
deployment must use a separate database, credentials, secret set, and encryption
boundary. Every protected request requires the deployment's exact
`X-Project-ID`. Authentication does not turn one database into a multi-tenant
store, and the header is a fail-closed routing assertion rather than a row-level
tenant boundary. Organizations requiring isolated projects must deploy isolated
control-plane instances.

## Protected assets

- Dataset inputs, expectations, slices, and canonical worker payloads.
- Target outputs, SQL, rows, per-case evidence, and release decisions.
- PostgreSQL contents, named volumes, logical backups, and transaction logs.
- API authentication material, provider credentials, database credentials, and
  private worker lease tokens.
- Idempotency keys and request digests used for durable submission coordination.
- Source, dependency locks, container definitions, migrations, and release-gate
  policy.
- Operational metadata that can reveal customer identifiers, model behavior,
  failure topology, or evaluation timing.

Content digests provide integrity evidence; they do not encrypt data or prove
who submitted it.

## Trust boundaries

### Client to API

HTTP bodies, headers, request identifiers, idempotency keys, pagination cursors,
and authentication material cross from an untrusted client into FastAPI. The
boundary performs authentication and authorization before application work,
strict media-type and JSON validation, body and complexity limits, and safe
error translation. Public responses expose summaries, never stored payloads,
lease data, database configuration, or raw exceptions.

Bearer credentials use the exact `cpk_` prefix followed by 43 URL-safe
characters. Runtime configuration retains only their SHA-256 digests. After
constant-time authentication, the boundary requires the configured project ID
and one of the bounded scopes `control-plane:read`, `control-plane:write`,
`control-plane:cancel`, or `observability:read` according to the operation.
Neither a token nor its digest may enter public output or telemetry.

The application does not terminate TLS. Compose publishes the API only on
`127.0.0.1`; any non-loopback deployment requires a trusted TLS-terminating
gateway. Authentication over plaintext or an untrusted network is unsupported.

### API and workers to PostgreSQL

PostgreSQL is the coordination clock and durable evidence store. API and worker
processes use a file-mounted password and do not run schema upgrades. A separate
one-shot migration service applies the exact Alembic revision before startup.
Workers receive canonical payloads and private leases directly from the
repository; those values must never cross the public API or telemetry boundary.

### Worker to target or provider

Provider responses are untrusted even when transport authentication succeeds.
They are size-bounded, strictly normalized, and converted to safe failure codes.
Worker recovery guarantees one durable evidence publication, but provider
invocation is at least once. A crash after an external effect and before
publication can cause a repeated call. Provider-side idempotency is required
when duplicate effects are unsafe.

### DataBridge to PostgreSQL replay

Candidate SQL crosses into a separately provisioned, read-only database role.
Parsing and allowlist policy precede replay. Each accepted statement executes in
a fresh read-only transaction with local time, row, column, cell, and byte
limits, followed by rollback. The fixture must be synthetic and fingerprinted;
production or customer databases are outside the supported boundary.

### Build and continuous integration

Package indexes, GitHub Actions, release archives, vulnerability databases,
container registries, and base images are external supply-chain inputs. Python
dependencies are locked with artifact hashes. Actions use full commit SHAs,
external images use manifest digests, and the Gitleaks archive is verified
against a committed SHA-256 value before execution. Scanner jobs receive no
repository secrets. The CodeQL job alone receives the minimum GitHub permission
needed to upload code-scanning results.

## Adversaries and failure sources

- An unauthenticated remote caller probing the HTTP surface.
- An authenticated caller exceeding its intended read or mutation authority.
- A caller attempting to infer or retrieve another project by crafting IDs.
- Malformed datasets, target responses, SQL, stored bytes, or cursors.
- A compromised target, provider endpoint, package, Action, scanner, or image.
- Accidental credential or evaluation-data disclosure through Git, logs,
  metrics, traces, errors, reports, command arguments, or support bundles.
- A stale, crashed, or delayed worker attempting to publish after lease loss.
- An operator applying the wrong migration, restoring the wrong database, or
  rotating only one copy of a credential.
- Resource exhaustion through large bodies, fan-out, expensive comparisons,
  connection pressure, queue growth, or repeated submissions.

## Threats, controls, and residual risk

| Threat | Implemented control and evidence | Residual risk |
| --- | --- | --- |
| Credential guessing or replay | Strict `cpk_` token parsing, digest-only configuration, constant-time comparison, bounded scopes, exclusion from public models, and sentinel tests. | The application does not provide distributed rate limiting or automated credential rotation. The edge gateway must enforce both. |
| Cross-project access | One deployment is one project; deployment, database, and secrets are isolated as a unit. The exact `X-Project-ID` and required scope are evaluated before resource access. | The header is not a row-level tenant boundary. Sharing a database across projects violates the model. |
| Request smuggling or parser ambiguity | Strict `application/json`, bounded body buffering, duplicate-key rejection, invalid UTF-8 rejection, nesting limits, and versioned safe errors. | An upstream proxy with conflicting HTTP parsing can still introduce ambiguity; use a maintained gateway with request normalization. |
| Sensitive API disclosure | Versioned response contracts expose bounded summaries. OpenAPI and sentinel tests reject payloads, keys, request digests, worker identities, lease tokens, SQL, rows, outputs, and exception text. | Identifiers, timestamps, metric names, aggregate values, and gate outcomes remain observable to authorized readers. |
| Job duplication or stale publication | Semantic idempotency, database-time leases, heartbeats, fenced attempts, bounded retry, and transactional immutable publication. | External target effects can repeat after lease loss. Cancellation cannot undo an effect already performed. |
| Evidence tampering | Canonical documents, content digests, create-once evidence rows, schema validation, transaction fencing, and read-time integrity checks. | A database administrator can alter both data and digest. Independent signed exports are not yet implemented. |
| SQL injection or database mutation | SQLAlchemy parameterization for control-plane data; DataBridge parsing, object/function allowlists, restricted role, explicit read-only transactions, limits, and rollback. | Database engine vulnerabilities and errors in reviewed dynamic identifier construction remain possible; least-privilege roles are mandatory. |
| Secret leakage in observability | Logs, metrics, and traces use bounded allowlisted fields. Prompts, outputs, SQL, rows, payloads, headers, credentials, idempotency keys, request digests, worker IDs, lease tokens, and raw exception text are denied. Sentinel tests exercise failure paths. | Metric labels and trace attributes can still become identifying if new fields bypass review. Telemetry schema changes require privacy tests. |
| Resource exhaustion | Bounded bodies, cases, evaluators, slices, gates, pages, reaper batches, attempts, backoff, database timeouts, response sizes, and Compose health behavior. | No distributed rate limiter, admission quota, or autoscaling policy is included. Loopback binding is not a substitute once deployed behind an edge. |
| Dependency or build compromise | Hashed uv lock, constrained build backend, locked dependency audit, weekly update PRs, full-SHA Actions, digest-pinned images, verified Gitleaks archive, CodeQL, and Trivy gates. | Vulnerability databases can lag, maintainers can publish malicious artifacts before detection, and an already trusted signing identity can be compromised. Pin review remains required. |
| Container escape or lateral movement | Fixed non-root UID, read-only application filesystems, `no-new-privileges`, all capabilities dropped, no worker port, internal database network, tmpfs with `noexec,nosuid,nodev`, and file-mounted secrets. | The Docker daemon, host kernel, and database container remain privileged trust anchors. Compose is a local topology, not a hostile multi-tenant sandbox. |
| Backup disclosure or failed restoration | Backups are classified like source evidence; recovery requires isolated restore, exact migration validation, digest checks, and credential rotation. | This repository does not schedule, encrypt, replicate, or retention-manage backups. Recovery objectives are not claimed. |

## Telemetry minimization rules

Telemetry is metadata, not an evidence export. New instruments must use an
allowlist and bounded cardinality.

Allowed examples include operation name, route template, HTTP status class, job
kind, public job status, stable safe failure code, evaluator name, aggregate
duration buckets, queue counts, and deployment-local health state.

Disallowed fields include request or response bodies, prompts, expectations,
target output, generated SQL, database rows, canonical documents, authorization
headers, API keys, database URLs, secret paths or contents, idempotency keys,
request digests, worker identity, lease token, raw cursor, raw exception text,
and caller-controlled strings as metric labels.

Request IDs may correlate safe operational events but must use the validated
bounded alphabet. They are not authentication or authorization evidence.
Diagnostic verbosity must not weaken the allowlist.

## Supply-chain acceptance rules

- Never use floating Action tags or container tags in executable CI.
- Review the source diff behind every pin update, including nested composite
  Actions.
- Keep workflow permissions job-local and remove permissions that are not
  required by the job.
- Do not pass provider, database, deployment, or repository secrets to scanners.
- Treat a scanner compromise as a source-read compromise even when the
  repository is public; rotate any token the job could access.
- Block known dependency vulnerabilities, critical runtime-image findings,
  fixable high runtime-image findings, high-risk deployment
  misconfigurations, secret-history findings, and CodeQL security findings.
- Do not suppress a vulnerability without a documented impact analysis,
  compensating control, owner, and expiry.

## Explicit residual risks

- TLS termination and certificate lifecycle are external to the application.
- Distributed rate limiting, per-principal quotas, and abuse detection are not
  implemented by this service.
- The supported isolation unit is one deployment and one project, not rows in a
  shared multi-tenant database.
- Compose does not provide encrypted backups, cross-region replication, host
  hardening, or a production orchestrator security boundary.
- Provider invocation remains at least once.
- Authentication cannot protect data already copied to logs, backups, or an
  incorrectly exposed database.
- No recovery point objective or recovery time objective is claimed until an
  operator deploys backups and repeatedly measures a restore drill.

## Review triggers

Review this model when adding a public deployment, tenant sharing, a provider
integration, a new credential type, telemetry attributes, arbitrary plugins,
file upload, remote artifact storage, a queue other than PostgreSQL, a new
scanner or Action, or a different backup architecture. Every new boundary must
identify assets, failure behavior, privacy tests, and recovery steps before it
is enabled.
