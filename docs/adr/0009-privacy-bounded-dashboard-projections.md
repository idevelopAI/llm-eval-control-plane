# ADR 0009: Privacy-bounded dashboard projections

- Status: accepted
- Date: 2026-08-28

## Context

Release summaries show whether configured gates passed, but an operator also
needs enough evidence to understand a regression: which score transitions
changed and how score, latency, and usage distributions differ between pinned
runs. The canonical decision and run records contain evaluation content and raw
operational samples that must not cross a general browser boundary.

Calculating distributions in the browser would require transferring sensitive
samples. Returning stored case documents would expose prompts, expectations,
outputs, SQL, rows, and failure context. Sending a long-lived bearer credential
from a public hosted page would also create an unsupported secret boundary.

## Decision

The API exposes two read-only analytical projections below one immutable release
decision:

1. A cursor-paged case projection selected by metric, exact optional gate slice,
   and optional transition class. Its response allowlist contains only case ID,
   slice labels, baseline/candidate score status and value, pass states, delta,
   and change class.
2. A fixed distribution projection for the same gate. It contains score
   coverage and fixed statistics plus latency and input/output/total usage-unit
   coverage and fixed statistics for both pinned runs. It never contains raw
   samples.

Both projections revalidate the immutable decision, pinned run identities,
digests, artifacts, and execution modes at read time. Sliced operational
statistics use exactly the gate's case IDs. Score statistics are exact because
the authorized case projection already exposes score values. Operational
statistics are suppressed below 20 measured samples while their coverage counts
remain visible. All API responses use `Cache-Control: no-store`.

The dashboard has two explicit sources. Fixture mode is the default and makes no
request. Local live mode is enabled only when the page itself uses plain HTTP on
a loopback hostname; its same-origin development proxy also accepts only an
explicit loopback HTTP target with a port. The read-only credential stays in a
component-scoped closure, is cleared on disconnect or authorization failure, and
is never persisted. Hosted live mode is rejected until a server-side session or
backend-for-frontend design is implemented.

Successful JSON is validated against strict runtime allowlists and then checked
for cross-response consistency before rendering. Request generations and abort
signals prevent stale decision, gate, filter, or pagination reads from winning a
race. The case and distribution projections recover independently after a
non-authorization failure, so validated sibling evidence remains available and
only the failed read is retried. An authorization failure remains session-wide:
it aborts the sibling read, removes prior evidence, and clears the volatile
credential. Browser pagination retains at most 500 redacted cases.

## Consequences

- Operators can explain a gate outcome without transferring raw evaluation
  content or operational samples.
- Small operational groups intentionally show coverage and a privacy-suppressed
  state instead of quantiles. This limits diagnosis but avoids presenting a
  small group as anonymous.
- Case IDs, slice labels, metrics, timestamps, digests, and aggregate values are
  still disclosed to authorized readers and must be classified accordingly.
- The API performs bounded reconstruction from immutable evidence on each read;
  it does not persist a second analytical document that could drift.
- The browser cannot inspect prompt-level or output-level evidence. A separate,
  more privileged workflow would need its own authorization and audit boundary.
- A hosted portfolio build can demonstrate the deterministic fixture but cannot
  accept a live bearer credential.

## Rejected alternatives

- **Return canonical run or decision evidence:** rejected because it crosses the
  content boundary and couples the UI to private persistence documents.
- **Transfer raw samples and aggregate in the browser:** rejected because
  network and browser memory would contain unnecessary operational evidence.
- **Enable bearer entry on any origin:** rejected because a hosted page is not a
  supported credential boundary.
- **Silently fall back to fixture data after a live error:** rejected because it
  can make stale demonstration data look operationally current.
- **Cache analytical responses in the browser or at an intermediary:** rejected
  because identifiers and metrics remain sensitive even after content redaction.
