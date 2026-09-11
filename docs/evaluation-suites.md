# Evaluation-suite API

A suite revision defines one immutable evaluation protocol shared by baseline
and candidate targets. The local API registers exact dependencies, enqueues
snapshot-pinned jobs, and returns redacted release evidence. All execution uses
the credential-free deterministic adapter; scores, latency, and usage are
synthetic, not live-model measurements.

## Endpoints and permissions

Every operation requires the configured project bearer credential and matching
`X-Project-ID`. Keep credentials in a secret manager or protected client
configuration, not command arguments, source files, logs, or screenshots.

| Method and route | Required scope | Result |
| --- | --- | --- |
| `POST /v1/suites` | `control-plane:write` | Register or replay an identical immutable revision (`201`) |
| `GET /v1/suites` | `control-plane:read` | Bounded metadata page |
| `GET /v1/suite-revisions/{revision}/{name:path}` | `control-plane:read` | Protocol metadata, with slash-safe names |
| `POST /v1/suite-runs` | `control-plane:write` | Enqueue a snapshot-pinned evaluation |
| `POST /v1/suite-comparisons` | `control-plane:write` | Enqueue comparison using only the pinned policy |

These are local control-plane endpoints. They are not added to the public Site,
which remains a synthetic, request-free build.

## Register a protocol

`POST /v1/suites` accepts these fields:

- `name` and positive integer `revision` identify a create-once revision.
- `dataset` is a resolved artifact reference: kind, name, revision, and SHA-256
  digest. Register the dataset first.
- `evaluators` binds each built-in `executor_name` to a resolved evaluator
  `artifact` and its exact `metrics` inventory. Approved run details expose
  evaluator artifacts and metric summaries; the application executor's
  `validate_suite` method also resolves these identities without invoking a target.
- `execution` specifies `deterministic_fake`, `offline_mock`, ascending case-ID
  order, one invocation per case, and concurrency one. Omission selects those
  defaults; live adapters, credentials, and alternative execution settings are
  rejected.
- `slices` declares unique labels that must exist in the resolved dataset.
- `gates` declares metric, direction, finite numeric threshold, optional
  nonnegative regression allowance, and optional declared slice.

The server computes the suite digest. Do not submit a digest, raw canonical
document, target configuration, or unknown fields. A metric must belong to
exactly one evaluator; gates and execution settings must match the supported
executor. Limits are 32 evaluators, 32 total metrics, 128 slices, and 64 gates.

Exact retries return the original registration even if current runtime
dependencies later change. Different content at the same name/revision returns
`409`; use a new revision for a policy change. Missing dependencies return `404`,
and invalid or drifted definitions return `422` without partial registration.

The list route supports `limit` (1–100, default 50), opaque `cursor`, and exact
`name` filtering. It returns identities and counts, not full protocol content.
Use the returned cursor unchanged with the same filters; invalid cursors return
`400`. Detail routes return explicit protocol metadata, never dataset cases,
expectations, outputs, scenario mappings, or raw stored documents.

## Run and compare

Send `POST /v1/suite-runs` with an `Idempotency-Key` header and a body such as:

```json
{
  "suite_name": "release/core",
  "suite_revision": 1,
  "target_name": "fake/candidate",
  "target_revision": 2
}
```

Optional `scenario_overrides` selects bounded deterministic scenarios per case.
It is part of the target's semantic identity and is never returned in API
responses. Dataset, evaluator, execution, and gate overrides are forbidden.
Run the baseline through the same suite revision using its own target identity
and idempotency key.

After both jobs succeed, send `POST /v1/suite-comparisons` with a new
`Idempotency-Key` and the exact resulting run IDs:

```json
{
  "suite_name": "release/core",
  "suite_revision": 1,
  "baseline_run_id": "baseline-run-id",
  "candidate_run_id": "candidate-run-id"
}
```

The run IDs above are placeholders; substitute IDs returned by your completed
jobs. Comparison rejects mixed pinned/unpinned evidence, different suite names,
revisions or digests, and any replacement policy. A valid comparison job may
succeed while its release decision is `failed`: job success means the policy
was evaluated, not that the candidate passed it.

Both submissions return `202` with a redacted job and a `Location` header under
`/v1/jobs/`. Poll that job or use the existing cancellation endpoint. New jobs
persist complete suite snapshots before execution; workers consume them after
restart without resolving a mutable alias. Exact nonterminal retries return
`202`; terminal retries return `200` with available run/decision summaries.
Reusing a key for different semantic input returns `409`.

## Compatibility and privacy

`GET /v1/runs/{run_id}` and `GET /v1/release-decisions/{decision_id}` include a
resolved `suite` artifact reference on pinned evidence. Historical unpinned
responses omit the field and retain their existing document shape; run and
decision collection items are unchanged. Evidence digest v3 binds the complete
suite identity and explicit execution mode; old v1/v2 evidence is not rewritten.

The dashboard client accepts and validates the optional suite reference without
changing its current review workflow. Suite authoring commands, dedicated
experiment-history queries, and suite-history dashboard views are not yet
implemented. Legacy `/v1/runs`, `/v1/comparisons`, and CLI submissions remain
suite-unpinned and cannot supply replacement policy for pinned evidence.

Suite identities and gates are sensitive project metadata. They are available
only to authorized reads and never become telemetry labels. Errors omit caller
values and internal exception details; logs and metrics retain route templates,
not suite names, scenario mappings, or canonical payloads.
