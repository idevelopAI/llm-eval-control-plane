# ADR 0011: Public synthetic example Site

- Status: accepted
- Date: 2026-09-01
- Accepted: 2026-09-02
- Supersedes: [ADR 0010](0010-owner-only-hosted-fixture.md)

## Context

ADR 0010 permits an owner-only hosted fixture. A public example can make the
release-evidence interaction available without granting access to an operational
control plane, but public access changes the audience and the failure impact. It
does not justify adding a hosted data path, credential boundary, model call, or
persistence layer.

The repository now has a production entry dedicated to deterministic synthetic
release evidence. The local development entry still supports loopback-only live
review. Those capabilities must remain separate at build time: a hidden control,
an origin check, or a disabled runtime branch would still ship the live code to
an untrusted browser.

Publication needs two distinct decisions. Deploying a build does not make it
public, and making it reachable does not authorize search indexing. The public
access review is recorded separately from the build so that both decisions
remain auditable.

## Decision

We publish a public Site whose application behavior is limited to a
deterministic synthetic fixture. It presents plausible release-gate, transition,
and distribution states, while clearly labeling the environment and evidence as
synthetic. It must not claim that the displayed records came from a customer,
project, provider, model invocation, or operational control plane.

The public production module graph is rooted in the dedicated public entry and
may contain only the fixture model, shared presentation components, local fonts,
styles, and framework code required to render them. The local live dashboard,
API client, credential controls, development proxy behavior, and disabled
server-side read foundation are not part of that graph. Installed workspace
dependencies do not establish a public capability; only the emitted production
graph and runtime behavior are accepted as release evidence.

The public Site has the following invariants:

1. It performs no application API request and exposes no application route
   handler. Requests to representative `/api` and `/v1` paths fail closed for
   `GET`, `POST`, `HEAD`, and `OPTIONS`.
2. It contains no model-provider client, endpoint, invocation path, or model
   credential. Every displayed result is computed from the checked-in synthetic
   fixture.
3. It contains no credential value, credential-entry control, authorization
   header construction, project credential header, or live-mode selection.
4. It uses no application persistence in browser or server code. This includes
   local storage, session storage, IndexedDB, cookies, background beacons,
   WebSockets, EventSource, databases, object storage, key-value stores, queues,
   durable objects, service bindings, or application secrets.
5. Its hosting manifest has only the opaque Site project identity and null D1
   and R2 fields. The generated Worker configuration has no application resource,
   environment, service, network, workflow, analytics, email, certificate, or
   scheduled-trigger binding.
6. Its dashboard visual system uses solid fills. Emitted CSS contains no linear,
   radial, or conic gradients.
7. The HTTP cache policy remains `private, no-store`, and responses retain the
   content security, framing, MIME-sniffing, referrer, capability, and
   cross-origin defenses established for the owner-only fixture.
8. Canonical and social metadata use the exact reviewed HTTPS origin. Robots
   metadata remains `noindex` and `nofollow` through deployment and access
   verification. Removing either directive requires a separate explicit public
   indexing approval after the reachable Site has been reviewed.

## Build acceptance gate

`pnpm run build` must finish by running the public-build verifier against the
complete generated `dist` tree. Publication is blocked unless the verifier
confirms all of the following:

- the public client entry is present, the local live entry is absent from the
  client manifest, and exactly one public application chunk exists for each of
  the client and server-rendered graphs;
- emitted application JavaScript contains no control-plane route, credential,
  live-mode, model SDK, or model endpoint marker;
- the public application chunks contain none of the prohibited browser storage
  or background-communication APIs;
- generated filenames and text contain no prohibited key or certificate files,
  source maps, recognizable private keys, access keys, tokens, or database URLs;
- emitted CSS is gradient-free;
- the hosting and Worker manifests have the exact expected empty binding shape;
- the server manifest contains no application route handler; and
- the framework prerender secret remains server-only and is absent from every
  client artifact.

The scanner is a release gate and defense in depth, not proof that an arbitrary
secret format is impossible. Structural graph separation, fixture-only runtime
tests, code review, and inspection of the exact generated artifact remain
required. A scanner rule must fail closed when an emitted framework format
changes and can no longer be classified.

The production runtime smoke check must start the built artifact, verify the
synthetic public copy and hardened response headers, prove that credential and
live-mode controls are absent, and probe the application route boundary. The
exact reviewed commit and its passing build and smoke results must be recorded
with the publication.

## Access verification and supersession

This ADR remained proposed while the Site was owner-only and while public access
was unverified. A deployment preview, an authenticated owner session, or a
platform sharing setting alone was insufficient evidence of public reachability.

Acceptance requires a deliberate access change followed by all of these checks
against the canonical production origin:

1. The hosting platform reports the intended public audience and no unexpected
   account, workspace, tenant, or resource binding.
2. A signed-out browser with no owner session receives the canonical page over
   HTTPS without a sign-in redirect or access challenge.
3. A second unauthenticated request context independently receives the same
   release and security behavior, including `no-store`, synthetic labeling,
   fixture-only controls, and `noindex`/`nofollow` metadata.
4. The deployed version is linked to the exact reviewed commit whose production
   build scanner and runtime smoke check passed.
5. Network inspection shows no application API or model request during initial
   render and every supported interaction.

The checks are recorded in the
[public Site release record](../operations/public-site-release.md). This ADR is
therefore accepted and supersedes ADR 0010. Public indexing remains a later,
independent approval and was not a condition for accepting the access change.

## Rollback

If any build, runtime, audience, metadata, or deployed-version check fails before
acceptance, public access must be removed immediately and the last verified
owner-only version restored. The Site must remain `noindex` and `nofollow`; the
failure is corrected in source and the full build, smoke, artifact, and access
verification sequence is repeated. An in-place production exception is not an
acceptable repair.

After acceptance, discovery of an unexpected request path, credential or secret
material, storage use, binding, route handler, non-synthetic evidence, indexing
change, audience drift, or unverifiable deployment identity requires the same
immediate access withdrawal. A follow-up ADR must document the incident-driven
policy change before republication; this historical decision is not silently
rewritten.

Access withdrawal cannot recall content already received by a public client.
The fixture-only and non-cacheable design limits that consequence, but it does
not replace rapid revocation and post-incident review.

## Consequences

- Anyone can inspect a realistic release-evidence workflow without receiving
  operational evidence or authority over a control plane.
- Public rendering does not create a supported hosted-live architecture. Any
  future operational data path requires a new decision, threat-model review,
  server-side identity boundary, read-only authorization, and separate rollout.
- The dedicated production graph and artifact scanner make absence of dangerous
  capabilities testable at the deployment boundary, at the cost of maintaining
  explicit marker and manifest rules as the framework evolves.
- Search discovery remains disabled until it is approved independently from
  public reachability.
- Public synthetic values, labels, metadata, and static assets should be treated
  as permanently observable even after rollback.

## Rejected alternatives

- **Ship the local live entry with its controls hidden:** rejected because the
  API and credential capability would still cross the public browser boundary.
- **Add a read-only public API or model-backed interaction:** rejected because it
  creates authorization, privacy, cost, abuse, and availability boundaries that
  this fixture does not need.
- **Persist preferences or fixture state:** rejected because the interaction is
  fully useful with transient component state and does not need a browser or
  server storage boundary.
- **Configure unused bindings for future work:** rejected because dormant
  resources expand deployment authority and make the public artifact harder to
  audit.
- **Permit gradients in dashboard CSS:** rejected because a uniform solid-fill
  rule is straightforward to verify and is part of the approved visual contract.
- **Enable indexing when access is changed:** rejected because reachability and
  search distribution are separate product and risk decisions.
- **Supersede ADR 0010 at merge or deployment time:** rejected because source
  intent and owner-session access do not prove unauthenticated public access.
