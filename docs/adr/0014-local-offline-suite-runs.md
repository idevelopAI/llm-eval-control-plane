# ADR 0014: Minimal local offline suite-run workflow

## Status

Proposed; authenticated run submission requires explicit approval before merge.

## Decision

Complete the local workflow with one selected, registered offline suite and two
fixed deterministic target identities: `fake/baseline` r1 and `fake/candidate`
r2. Both execute the existing deterministic adapter without scenario overrides;
different names are not represented as different models. No providers, hosted
write routes, new backend endpoint, schema migration, or credential scope change
is introduced.

Reuse the bounded local-job transport established by
[ADR 0013](0013-local-comparison-submission.md), while keeping run and comparison
clients separate. Each client exposes its single fixed submission endpoint and
read-only refresh of its own accepted job kind. The read session remains
separate from a one-request write vault. Explicit selection and submission are
required; input and idempotency key freeze for uncertain-response retries.
Job identity, monotonic progress, terminal state, project, and HTTP loopback
restrictions fail closed. Authorization loss clears the entire session.

The run client retains only the validated job. If a terminal replay includes
a run summary, validate its identity against the selected suite/dataset/mode,
fixed target identity, and job resource; discard metric content. Completed
evidence is independently read through the existing strict suite-history
projection. **Show completed runs** reloads the same suite and resets filters;
it does not select comparison inputs or submit a comparison automatically.

Provide a public one-case starter dataset and an interactive registration helper.
The helper registers a create-once dataset and suite using a hidden credential
prompt, fixed routes, explicit loopback origin, no environment proxy, and no
redirect following. It does not provision credentials or execute evaluations.
Registration is repeatable but not transactional across those two resources.

## Limits and verification

Retain the browser/extension and broad-write-scope risks documented in ADR 0013.
Closing or changing history loses the in-memory retry key, not the accepted job.
Use persisted history or the local job API for recovery. Background polling,
reload recovery, cancellation controls, suite authoring, and paid provider
integration are deliberately outside this completed local scope.

Tests cover the client boundaries, credential lifecycle, same-key retry,
duplicate-submit suppression, job transitions, and accessible controls. The
production verifier rejects run-write UI in the hosted fixture. A real local
PostgreSQL/API/leased-worker browser smoke verifies two starter runs, a lost
response with same-key recovery, one comparison, and detailed gate review.
