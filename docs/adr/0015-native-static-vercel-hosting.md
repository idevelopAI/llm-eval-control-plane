# ADR 0015: Native static Vercel hosting

- Status: proposed; pending security review and merge
- Date: 2026-09-27
- Supersedes: the hosting implementation in [ADR 0011](0011-public-example-site.md)

## Context

The public demo moved to Vercel on September 25, but the repository retained a
second production build, an old hosting manifest and plugin, Worker dependencies,
and an unused platform-authenticated read prototype. Those paths could mislead
maintainers into deploying obsolete source or treating dormant code as a
supported hosted API. The prototype was never composed into an application route.

## Decision

1. Native Next.js is the only application framework build. `pnpm run build`
   exports the fixture into `out/`; `build:vercel` is a compatibility alias.
   Only verified static assets and the generated CDN policy are uploaded.
2. Every non-development configuration phase uses `output: 'export'`. Production
   cannot be opted into the local API by an environment flag. Verification rejects
   proxy rewrites, application routes, server actions, unexpected client entries,
   request/persistence capabilities, secret markers, gradients, and stale metadata.
3. `next dev --webpack --hostname 127.0.0.1` substitutes the local live entry and
   rewrites only the existing health, OpenAPI, and v1 paths to the validated
   loopback HTTP API. Credentials, API scopes, deterministic execution, and
   manual submission workflows retain their existing boundaries.
4. Remove the retired Sites manifest, plugin, Vinext/Cloudflare tooling, and
   unused hosted read adapter with its platform identity dependency. Do not
   migrate that identity header to Vercel or introduce hosted live access.
5. Preserve the existing fixture-only, no-provider, no-storage, no-indexing,
   solid-fill, and restrictive-header policies. Local preview verifies the
   static artifact, not Vercel's edge implementation; live deployment checks
   remain mandatory, including empty `OPTIONS` exceptions without CORS grants.
6. Rollback uses a verified previous Vercel deployment or withdrawal of public
   access. The old Site remains privately recoverable with owner-only access;
   it is not an active public fallback and has not been deleted.

## Verification

The dashboard gate checks generated API types, lint, TypeScript, unit and UI
tests, static-policy negative tests, the complete static build, loopback static
preview probes, and a native development smoke test against a temporary mock
API. Repository security tests reject reintroduced hosting dependencies,
configuration, or legacy identity headers. Frozen installation, vulnerability
audit, and redacted secret scanning remain required.

The [deployment record](../operations/vercel-static-demo.md) identifies the
already-live Vercel artifact separately from this source cleanup. Merging this
change does not publish source, redeploy Vercel, change the old host's access,
or add a service or charge.

## Consequences

There is one production artifact and fewer deployment dependencies. Native
development preserves local functionality without shipping it to visitors.
Tests for the removed, unused adapter are removed with it; active local and
static boundaries retain their tests and gain native build/proxy coverage.
Historical ADRs, release evidence, and generated-cache ignore rules remain
explicitly historical. Provider-domain denial rules remain security checks,
not integrations. Any future hosted live data path requires a new architecture
decision and security review.
