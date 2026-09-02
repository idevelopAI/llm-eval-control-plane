# Public Site release record

- Status: accepted
- Verification date: 2026-09-02
- Canonical origin: <https://llm-eval-control-plane.nick0ne.chatgpt.site>
- Reviewed repository commit: `526401f75720194e1781d34f5a158cadde266e18`
- Hosted version: `4`
- Hosted source commit: `ae4fa0f6d70b1d5af004b421e16cf63b7be3b9e1`
- Hosted archive digest: `sha256:71736f748072424d8c42616251c975f9fb196c150eaa30345890a06d9be3ec82`

This record closes the publication gate in
[ADR 0011](../adr/0011-public-example-site.md). It records the exact reviewed
source, hosted artifact, audience, and unauthenticated behavior accepted for the
public synthetic example. It does not authorize search indexing or a hosted
live-data path.

## Reviewed source and artifact

[Pull request #21](https://github.com/idevelopAI/llm-eval-control-plane/pull/21)
merged the Phase 9 source into the reviewed repository commit above. All required
GitHub checks passed before merge, including the dashboard, API, release,
DataBridge, worker-recovery, dependency, secret-history, container, static
analysis, CodeQL, packaging, and Python-version gates.

The deployed Site reports version 4 with the hosted source commit and archive
digest above. The archive contains 113 files and is 3,072,000 bytes. Its source
was prepared from the merged Phase 9 state. The production build verifier and
runtime smoke check passed for that reviewed state before the version was saved
and deployed.

## Audience and hosted authority

The hosting platform reported all of the following during verification:

- the Site is active and its access mode is `public`;
- the current operator is the owner;
- there are no external visitors;
- there are no workspace or tenant groups;
- the hosting manifest has `d1: null` and `r2: null`; and
- the hosted environment contains zero entries and zero secrets.

The owner allowlist remains administrative metadata. It does not restrict the
public audience or grant a runtime capability to the application.

## Unauthenticated response verification

Two separate cookie-free HTTP/2 request processes, using distinct user-agent
identities, independently requested the canonical origin. Both received the same
25,078-byte public HTML response with `HTTP/2 200`, without a redirect, sign-in
challenge, access error, live-data control, or credential control. Each response
contained the canonical origin, the public-example and synthetic-data labels,
and `noindex` plus `nofollow` metadata.

Both responses carried the reviewed security policy:

- `cache-control: private, no-store, max-age=0`
- `strict-transport-security: max-age=63072000; includeSubDomains; preload`
- `x-content-type-options: nosniff`
- `x-frame-options: DENY`
- `referrer-policy: no-referrer`
- `cross-origin-opener-policy: same-origin`
- `cross-origin-resource-policy: same-origin`
- a permissions policy denying browsing topics, camera, geolocation, microphone,
  payment, and USB access
- a self-confined content security policy with `base-uri 'none'`,
  `frame-ancestors 'none'`, `form-action 'self'`, `object-src 'none'`, and
  `connect-src 'self'`

## Browser interaction and request boundary

A fresh browser tab loaded the canonical HTTPS page without an access challenge.
The rendered page exposed no form, input, textarea, credential entry, or live-mode
control. Its canonical metadata matched the production origin, and its robots
metadata remained `nofollow, noindex`.

The review exercised every application-owned interaction:

- all five slice lenses;
- all four release-gate selections;
- the failed-gate review action;
- the case-evidence expansion; and
- all four internal evidence-section links.

The browser observed exactly 11 same-origin static assets: six scripts, three
fonts, one stylesheet, and one favicon. The inventory was identical before and
after the interaction sequence, with no new request URL and no API, model,
analytics, storage, or external origin. The only application chunk was the
dedicated public release dashboard; the production build verifier separately
proved that live dashboard and credential modules were absent from the emitted
graph.

## Acceptance and continuing constraints

Public reachability is accepted for the deterministic synthetic fixture. Search
indexing remains disabled and is a separate decision. Hosted live data, model
calls, application persistence, runtime secrets, and resource bindings remain
unsupported.

Any audience drift, unexpected request path, credential or secret material,
storage use, binding, route handler, non-synthetic evidence, indexing change, or
unverifiable deployed source triggers the rollback procedure in ADR 0011.
