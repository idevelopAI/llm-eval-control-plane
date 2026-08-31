# ADR 0010: Owner-only hosted fixture

- Status: accepted
- Date: 2026-08-29

## Context

The dashboard's deterministic fixture is useful as production-build evidence,
but hosting the local live workflow unchanged would move a long-lived API
credential and project assertion into an untrusted browser boundary. A public
page could also be indexed or cached even when it contains only demonstration
data. Deployment metadata, source transfer, and runtime configuration must not
become a second path for secrets to enter the repository.

The hosted surface therefore needs a useful review artifact without implying
that it is connected to operational evidence. It must preserve the loopback-only
live policy, fail closed on hosted origins, and make its access and cache
properties explicit.

## Decision

The production Vinext build is deployed as an owner-only Site in deterministic
fixture mode. The hosted browser makes no control-plane request and cannot
render the bearer or project entry form. Local live review remains available
only when the page itself uses plain HTTP on a loopback hostname.

The Site has these release invariants:

1. The hosting manifest stores only the opaque Site project identity and null
   optional resource bindings. It contains no credential, endpoint secret,
   session state, or database value.
2. Each saved Site version is built from and linked to the exact pushed commit.
   The generated browser artifact is scanned for common credential formats
   before publication.
3. Deployment is permitted without another sharing decision only when the
   platform reports the caller as owner, exactly one allowed account, no
   external visitor, and no workspace or tenant group.
4. HTML and future API responses are private and non-cacheable. Production
   responses emit a self-confined content security policy, transport security,
   clickjacking, MIME-sniffing, referrer, capability, and cross-origin isolation
   defenses.
5. Canonical, Open Graph, and X metadata use the exact production HTTPS origin,
   never a forwarded request header. The private fixture emits `noindex` and
   `nofollow`, and its social image dimensions are verified in tests.
6. Hosted live access remains disabled. A future implementation must be a
   stateless, same-origin server boundary with platform-provided identity, a
   fixed server-side project assertion, and a separately provisioned read-only
   service credential. It must expose explicit bounded GET routes instead of a
   generic reverse proxy.

## Implemented disabled foundation

The repository contains an isolated server-only foundation for that future read
boundary. Its identity, configuration, request-provenance, operation-allowlist,
URL-construction, bounded-JSON, and response-projection checks are pure; the
fixed read executor receives its network dependency explicitly. No runtime or
platform binding composes these modules, no application route invokes them, and
no Site secret is configured. The production Site therefore remains the same
owner-only, zero-request fixture.

Activation remains blocked until all of these conditions are met:

1. Server-only secret binding and request-dispatcher behavior are verified in
   the production Worker runtime.
2. A separate read-only service token is provisioned outside source, build
   output, the hosting manifest, browser code, and logs.
3. Owner-only private Site access is reverified immediately before activation
   and deployment.
4. Explicit route adapters expose only the allowlisted GET operations and deny
   every other method, including `HEAD` and `OPTIONS`, without introducing a
   generic path or proxy.

## Consequences

- The owner can verify a real production Worker deployment without placing an
  API credential, project assertion, or private evaluation evidence in browser
  code.
- Repository visitors can audit the exact header policy, metadata contract,
  fixture behavior, and deployment boundary even when they cannot open the
  owner-only Site.
- Search engines and intermediaries are instructed not to index or retain the
  private fixture. Platform access control remains the authoritative viewer
  boundary.
- Sharing the Site with additional viewers is a separate deliberate access
  change. The fixture remains safe if shared later because it has no live data
  path, but the effective audience must be reviewed before publication.
- The static CSP currently permits framework-required inline script and style
  execution while forbidding `unsafe-eval`, remote script origins, external
  connections, embedding, and object content. A future per-request nonce must be
  validated against the production Worker runtime before replacing it.

## Rejected alternatives

- **Enable the local bearer form on HTTPS:** rejected because transport security
  does not make browser-held service credentials an acceptable hosted boundary.
- **Infer the canonical origin from forwarding headers:** rejected because those
  headers are caller-controlled unless a specific trusted proxy contract is
  established.
- **Store a service token in the hosting manifest or public environment:**
  rejected because both paths can expose the value through source, build output,
  or client code.
- **Deploy a generic API reverse proxy:** rejected because route growth could
  silently expose mutations, raw evidence, metrics, or future privileged
  endpoints.
- **Publish to a broader audience by default:** rejected because deployment and
  access policy are separate decisions even for a fixture-only artifact.
