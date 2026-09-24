# ADR 0013: Explicit local comparison submission

## Status

Proposed; implementation requires explicit security-review approval before merge.

## Context

Suite history already exposes immutable completed run identities and detailed
historical decisions. Operators still have to leave the dashboard to compare
two existing runs. Adding submission must not silently upgrade the existing
read-only browser session or introduce a hosted write path.

## Decision

- Keep suite browsing and gate review read-only. Add a separate, local-only
  comparison form with explicit baseline/candidate selection and direction.
- Preflight exact suite name/revision/digest, dataset name/revision, and execution
  mode from validated history metadata. Reject equal run IDs and non-offline
  suites. Include runs with case failures so existing coverage gates remain
  authoritative. Server-side canonical evidence validation is unchanged.
- Require a separately entered same-project `control-plane:write` credential
  for each submit/retry. Retain it only in a separate volatile vault during the
  request. Replace the password element immediately and clear the vault on
  completion, lost access, navigation, or unmount. Do not provision credentials
  or alter existing principal scopes.
- Allow only `POST /v1/suite-comparisons` and read-only refreshes of the accepted
  job. Require HTTP loopback at the request boundary, exact same-origin routes,
  no redirects/cookies/referrers/cache, strict response field allowlists, a
  2 MiB response limit, and a 30-second request deadline. Server errors remain
  generic. `401`/`403` clears both submission state and the read session.
- Generate one idempotency key per explicit comparison attempt. Once submitted,
  freeze inputs; uncertain-response retries preserve both key and body. Do not
  automatically retry writes. Do not infer failure from an aborted browser read.
- Refresh job status only on demand with the read credential. Match job ID,
  resource ID, creation time, attempt limit, and monotonic attempt/update values.
  Do not regress a terminal status. A succeeded comparison job can have a failed
  release gate; failed/canceled jobs do not open a successful result.
- Before gate review, match the returned decision to the accepted job resource
  and selected suite, dataset, execution mode, ordered target identities, run
  IDs, and both result digests. Existing detailed review performs its own identity
  verification before fetching case/distribution evidence.
- Keep all write controls/client code out of the public fixture graph. Extend
  public artifact checks for comparison credential UI and idempotency markers.
  No public route, runtime binding, provider request, or Site deployment changes.

## Consequences and limits

The local browser temporarily handles a write-scoped bearer credential. A
compromised browser/extension or local development origin can steal it during
entry; one-request retention is risk reduction, not isolation against XSS.
Only use a trusted loopback development environment. The existing write scope
is broader than comparison submission; no claim of a comparison-only server
credential is made. The client simply exposes no other write operation.

Changing history or leaving the form clears selections and the in-memory
idempotency key; accepted jobs continue. Persisted history and the job API are
the recovery sources. This design adds neither browser persistence, automatic
baseline choice, run execution, job cancellation, nor background polling.

## Validation

Tests cover origin/project restrictions, exact request bodies and credentials,
compatibility, malformed/private response rejection, response bounds, deadlines,
same-key retry, duplicate-submit suppression, cancellation on unmount, stale
history reset, authorization loss, job identity, terminal decision provenance,
and accessible form controls. Existing production artifact/runtime checks must
continue to prove the hosted build is fixture-only and fail-closed.
