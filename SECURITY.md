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
