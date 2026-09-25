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
- explicitly opened suite history for a selected immutable protocol revision,
  showing completed run metadata and release decisions pinned to its digest;
- independent exact-target and directed baseline/candidate-pair history filters;
- explicit offline suite-run submission with fixed deterministic target choices,
  a one-request write credential, and manual job-status refresh;
- explicit comparison of existing compatible suite runs with a separate,
  one-request write credential and manual job-status refresh;
- transition filters over redacted case evidence;
- cursor-based case pagination, bounded to 100 cases per request and 500 cases
  retained by the browser view;
- fixed score and operational distributions, with small operational samples
  suppressed by the API; and
- explicit loading, empty, authorization, network, and inconsistent-evidence
  states without silently substituting fixture data. A non-authorization failure
  in the case or distribution projection leaves the successful sibling visible
  and gives the failed panel its own retry action.

### Browse suite history

After connecting to the local API, select **Browse suite history**. Select a
registered **Suite revision** to inspect its exact digest, execution mode, gate
count, evaluated target revisions, and baseline/candidate run IDs. The panel
does not fetch case content, execute evaluations, or automatically select a
baseline. A separate explicit comparison form is available below the history.

Catalog, run, and decision pages load 20 records at a time and retain at most 100
records each. Use **Load more suites**, **Load older runs**, or **Load older
decisions** explicitly; the panel does not poll or prefetch. Runs and decisions
are ordered newest first, preserving timestamp microseconds and ID tie-breaks.
Older records remain available through the authenticated API after the display
limit. **Refresh suites** resets the view and loads the current first pages.

Unpinned legacy evidence, queued jobs, and canceled jobs are outside suite
history. A **Blocked** release means policy failed, not that its worker failed.
Empty, loading, and retry states never substitute synthetic evidence for a live
response. Invalid ordering, duplicate IDs, inconsistent suite pins, or repeated
cursors fail closed; a pagination failure preserves already verified records.
Changing revisions aborts superseded reads. Any `401`/`403` clears both history
and the release view and requires a new local connection.

Select **Review gates** on a release decision to open its detailed review,
including decisions outside the newest 20. Before requesting case or distribution
evidence, the client checks the selected row against the decision ID, digest,
status, timestamp, baseline/candidate run IDs, and complete suite pin. It then
opens the first failed gate (or first gate when all pass), resets the case filter,
and focuses the review surface. The detailed view keeps the existing redacted
case and aggregate-only distribution boundaries.

The recent-decision picker remains bounded to its original collection plus, at
most, the currently selected historical decision. An older selection is labeled
**Suite history**; choosing a recent decision removes the extra option. Opening
another row cancels the prior request. A missing or inconsistent historical
decision preserves the previous verified review with an explicit error; retry
through that row. Authorization failure clears both panels immediately.

History rows remain metadata-only until explicitly opened. The hosted synthetic
dashboard has no suite browser and makes no suite API requests.

### Filter history by target

Opening a suite also loads the first [target and target-pair group
pages](../docs/evaluation-suites.md#target-grouped-history) for that exact suite.
Choose **Run target** to filter evaluation runs, or **Decision target pair** to
filter release decisions. These filters are independent: filtering runs does not
implicitly choose a comparison baseline. **All targets** and **All baseline →
candidate pairs** restore their respective suite-wide histories.

Selections bind the complete name, revision, and digest; the same name and
revision with different digests remain different options. Option labels shorten
digests, extending them to disambiguate loaded options. Selected pins are shown
in full. Pair direction matters: A → B and B → A are separate groups. Only
pairs observed in persisted comparisons are offered, not every possible target
combination.

Group pages load 20 records at a time, in bytewise name, numeric revision, and
digest order (baseline first, then candidate for pairs). **Load more targets**
and **Load more pairs** explicitly extend their own catalogs, each capped at
100 groups. They do not reload history. Further groups remain available through
the API; loaded counts are not suite totals.

Changing either filter cancels pending metadata reads and reloads both history
columns from their first pages, preserving the other filter. Previous rows are
cleared while loading so they cannot appear under a new selection. Pagination
keeps the active full-identity filters; selecting a different suite or
**Refresh suites** resets both filters and all cursors. Failed pagination retains
already verified records; refresh to recover from an inconsistent cursor.

Group responses use strict runtime metadata allowlists, exact suite-pin checks,
and ordering/duplicate validation, including across pages. Filtered run responses
must match the requested target. The API enforces pair filtering; before loading
cases or distributions, **Review gates** additionally verifies the decision's
resolved baseline and candidate against the selected pair. A mismatch leaves
the previous verified review visible with an error. Authorization failure on any
group or history read clears the entire local session.

![Local target-grouped history using synthetic test evidence](../docs/assets/suite-target-history.png)

_Captured from the local dashboard with intercepted synthetic test responses;
no real credential, provider, or hosted API is involved._

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

## Create a comparison locally

If no completed runs exist yet, start with the [smallest offline
suite](#run-the-smallest-offline-suite) below.

1. Connect the loopback dashboard with a `control-plane:read` credential and
   browse an exact suite revision. Use **All targets** and load any older runs
   needed before selecting; only the bounded history already loaded is offered.
2. Choose **Choose runs to compare**, then explicitly select **Baseline run**
   and **Candidate run**. The form checks the exact suite pin, dataset revision,
   and execution mode; identical run IDs and non-offline suites are rejected.
   Runs with case failures are allowed because coverage gates must evaluate
   them rather than silently hiding failures.
3. Review both run IDs, target pins, result digests, direction, and project.
   Enter a separate same-project credential with `control-plane:write` and
   choose **Submit comparison**. This calls only the existing
   `POST /v1/suite-comparisons`; it does not invoke a target, submit evaluations,
   cancel jobs, register suites, or change permissions. The server independently
   validates the canonical evidence and applies the pinned suite policy.
4. Use **Refresh comparison status** explicitly. Job reads use the original
   read-only credential, not the write credential. There is no polling.
5. When the job succeeds, choose **Review comparison gates**. Before delegating
   to detailed gate review, the dashboard verifies the decision's job resource,
   suite pin, dataset, mode, baseline/candidate IDs, targets, and result digests.
   A successful job can correctly produce a blocked release.

The password field is replaced immediately after submit and the separate write
vault is cleared when the request settles, when history changes, or on unmount.
Neither credential enters React state, browser storage, logs, URLs, or screenshots.
The browser cannot verify a credential's scopes; the API enforces authorization.
Any `401`/`403` clears the entire local session. Requests reject redirects and
cookies, use the exact loopback origin, and bound comparison responses to 2 MiB
and 30 seconds. Error bodies and exception text are never displayed.

After a lost or invalid response, **Retry same comparison** reuses the same
idempotency key and immutable inputs; re-enter the write credential. An uncertain
response does not prove that submission failed. Once a job is known, refresh
that job rather than submitting again. Its ID/resource identity and immutable
fields must stay consistent across refreshes.

Refreshing, paging, filtering, selecting another suite, or disconnecting closes
the comparison form and aborts browser requests. This does **not** cancel a
durable job already accepted by the API. The in-memory submission key is lost
when the form/tab closes; use persisted history or the local job API to recover
evidence before starting another comparison. No background recovery or storage
of submission state is implemented.

These controls and their write client are excluded from the hosted build. See
[ADR 0013](../docs/adr/0013-local-comparison-submission.md) for the boundary.

![Explicit local baseline and candidate selection with an empty write-credential field](../docs/assets/suite-comparison.png)

_Captured from the local dashboard with intercepted synthetic test responses,
before entering the test write credential. No real credential, provider, or
hosted API is involved._

## Run the smallest offline suite

Start the authenticated local API, PostgreSQL database, and worker as described
in [Run locally](#run-locally). From the repository root, register the public
synthetic starter fixture in an interactive terminal:

```bash
uv run python scripts/seed_local_starter.py --project YOUR_LOCAL_PROJECT_ID
```

Replace the project placeholder with the deployment's configured ID. The script
prompts for a local `control-plane:write` credential without echoing it; never
paste it into the command line, environment, Git, or a screenshot. Use
`--origin http://127.0.0.1:PORT` only when your local API uses a different port.
Only explicit HTTP loopback origins are accepted. Environment proxies and
redirects are disabled. The script registers `starter/echo` r1: one synthetic
echo case, one built-in exact-match evaluator, and one gate requiring score 1.
It does not create credentials or start jobs. Identical registration replays
are safe; conflicting immutable revisions fail without overwrite. The two
registrations are not one transaction: if suite registration fails, the
successfully registered dataset remains.

1. Connect the local dashboard with a read-only project credential and choose
   **Browse suite history → Suite revision → starter/echo r1**. The history
   browser is available even when no release decisions exist.
2. Choose **Start an offline run**, explicitly select `fake/baseline` r1, and
   enter the separate same-project write credential. Choose **Submit offline
   run**. No target is selected or executed automatically.
3. Use **Refresh run status** to read the accepted job with the original read
   credential. A worker must be running. No background polling occurs.
4. Once it succeeds, choose **Show completed runs**. This reloads the same suite
   with both history filters reset; it does not choose a baseline for you.
5. Repeat for `fake/candidate` r2, then use **Choose runs to compare** and follow
   the comparison workflow above. Both fixed targets use identical deterministic
   behavior with no scenario overrides, so this starter should produce an equal,
   passing comparison. The names/revisions are identities, not different models.

Run submission uses only the existing `POST /v1/suite-runs` endpoint. The API
pins and validates the suite snapshot; the UI never replaces the dataset,
evaluators, gates, execution settings, or scenario map. Run requests share the
comparison client's loopback, same-project, redirect, response-size, deadline,
and sanitized-error boundaries. The optional terminal run summary is checked
for identity, then discarded; completed evidence is loaded through validated
suite history. A succeeded job can still contain failed cases, and is not itself
a release decision.

The write field is replaced on submit and its separate vault clears when the
request settles. Lost responses use **Retry same run**, with identical input and
idempotency key and a newly entered credential. Closing, paging, refreshing,
changing suite/filter, or disconnecting discards form state and aborts browser
requests, but never cancels an accepted durable job. Before starting another run
after losing the form, recover through the local job API or completed history.
There is no reload recovery, cancellation UI, suite editor, or provider-backed
execution in this scope. Any `401`/`403` clears the entire local session.

![Local offline run form with an empty write-credential field](../docs/assets/suite-run.png)

_Captured against a disposable local PostgreSQL/API/worker deployment using only
the synthetic starter case and test principals, before entering the write
credential. No provider or hosted API is involved._

See [ADR 0014](../docs/adr/0014-local-offline-suite-runs.md) for the boundary.

## Hosted build boundary

The public Vercel demo uses `pnpm run build:vercel`. This separately produces
`out/` with a native Next.js static export and a CDN-only `vercel.json` policy.
The build fails on dynamic routes, server actions, unexpected client entries,
local write controls, provider endpoints, secret markers, source maps,
symlinks, gradients, or stale hosting metadata. Only public assets are uploaded;
`.next/`, repository source, local environment files, and worker output are not.
The existing response-header policy is applied at Vercel's CDN, with explicit
API denial and no catch-all homepage fallback. See the
[deployment and rollback guide](../docs/operations/vercel-static-demo.md).

The existing Sites-compatible build remains available for rollback:

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
