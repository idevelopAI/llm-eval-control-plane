# Release evidence dashboard

This dashboard turns immutable release decisions into a bounded review surface:
gate outcomes, score-only case transitions, and aggregate score, latency, and
usage-unit distributions. It never requests or renders prompts, expectations,
target outputs, SQL, rows, provider responses, or exception text.

## Data-source modes

The initial **fixture** mode is deterministic and makes no API requests. It is
safe for a public example environment and is always labeled as synthetic data.

The production homepage imports a dedicated fixture-only component. API client,
credential, live-mode, and loopback-proxy modules are absent from its client and
server-rendered application chunks. Development resolves the homepage to the
local live-capable component only while `vinext dev` is running; production
builds do not use that development substitution.

The **local live** mode is available only when the dashboard itself is served
over plain HTTP on `localhost`, `127.0.0.1`, or `[::1]`. It connects through the
same-origin Vite proxy to an explicit loopback control-plane origin. A hosted
origin never renders the bearer-entry form.

Live mode supports:

- the newest 20 immutable release decisions, ordered newest first;
- decision and gate selection with cancellation of superseded requests;
- transition filters over redacted case evidence;
- cursor-based case pagination, bounded to 100 cases per request and 500 cases
  retained by the browser view;
- fixed score and operational distributions, with small operational samples
  suppressed by the API; and
- explicit loading, empty, authorization, network, and inconsistent-evidence
  states without silently substituting fixture data. A non-authorization failure
  in the case or distribution projection leaves the successful sibling visible
  and gives the failed panel its own retry action.

## Credential boundary

Use a project credential with only `control-plane:read`. The raw value stays in
one component-scoped closure for the lifetime of the tab. The dashboard does not
write it to local storage, session storage, cookies, URLs, React state,
logs, or rendered markup. Disconnecting, unmounting, or receiving `401`/`403`
drops the retained reference and aborts active reads.

The browser client sends credentials only to the same origin. Development proxy
configuration accepts only explicit loopback HTTP origins with a port. Responses
use `no-store`, redirects are rejected, referrers are suppressed, server error
messages are discarded, and successful JSON is checked against strict runtime
allowlists before it reaches the view model.

Hosted live access is intentionally unsupported in the public example. A later
hosted version requires the stateless, platform-authenticated backend-for-frontend
boundary described below; do not enable browser bearer entry on a public origin.

## Hosted build boundary

`pnpm run build` runs an artifact verifier after compilation. The verifier fails
if the live dashboard enters the public module graph; if application chunks
contain control-plane routes, credential markers, model-provider endpoints, or
browser persistence; if the output contains secrets, source maps, credential
files, gradients, or unexpected runtime bindings; or if a server-only prerender
secret reaches a client artifact.

`pnpm run smoke:public` starts the built runtime on a temporary loopback port,
checks the hardened response and non-cacheable fixture HTML, and proves that
GET, POST, HEAD, and OPTIONS requests against representative paths below `/api`
and `/v1` all resolve to 404. Together with the generated route-manifest check,
these gates verify the shipped artifact rather than relying only on source-level
origin checks. The accepted public-access and rollback policy is recorded in
[ADR 0011](../docs/adr/0011-public-example-site.md), with the exact deployed
artifact and unauthenticated review captured in the
[public Site release record](../docs/operations/public-site-release.md).

## Implemented disabled foundation

Tested server-only helpers now define the future hosted read boundary: platform
owner identity, private configuration, same-origin request provenance, four
allowlisted GET operations, bounded JSON reads, and strict response projection.
They are not connected to a runtime binding or application route, and no Site
secret is configured. The hosted dashboard remains a zero-request synthetic
example with no live behavior change.

Enabling these helpers requires verified server-only secret binding and request
dispatch in the production Worker runtime, a separately provisioned read-only
service token, withdrawal of public fixture access followed by reverified
owner-only private access, and explicit route adapters that deny every non-GET
method (including `HEAD` and `OPTIONS`).

## Run locally

Start the control-plane Compose stack first, including its project-bound
authentication configuration. Then install the locked frontend dependencies and
start the development server:

```bash
cd dashboard
pnpm install --frozen-lockfile
CONTROL_PLANE_DEV_ORIGIN=http://127.0.0.1:8000 pnpm dev
```

Open the loopback URL printed by the development server. Select **Use local live
data**, then enter the exact project ID and a read-only credential obtained from
your local secret manager. Do not put the credential in `.env`, a command,
source code, screenshots, test fixtures, or Git.

`CONTROL_PLANE_DEV_ORIGIN` defaults to `http://127.0.0.1:8000`. Any configured
value must remain an explicit HTTP loopback origin with a port.

## Validate

```bash
cd dashboard
pnpm run api:check
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run build
pnpm run smoke:public
```

The test suite includes runtime-contract rejection, credential non-persistence,
origin restrictions, stale-response cancellation, authorization clearing,
decision identity checks, isolated projection recovery, pagination boundaries,
automated accessibility checks for the major UI states, a solid-fill visual
contract, production artifact inspection, and built-runtime route probes.
