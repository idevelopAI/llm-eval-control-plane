# Static demo deployment

The public demo serves synthetic release evidence from a static export. It does
not connect to the control plane, submit runs, call model providers, retain
browser credentials, or use a hosted database. Local operation is unchanged.

## Build and publish

From `dashboard/`, using the pinned package manager and supported Node version:

```bash
pnpm install --frozen-lockfile
pnpm run check
```

The final check runs `pnpm run build:vercel`, generating a native Next.js static
export in `out/` plus its reviewed `vercel.json`. The artifact verifier requires
static routes, no server actions, only the public fixture client entry, and the
exact CDN policy. It rejects symlinks, unexpected file types, source maps,
credential markers, local write controls, provider endpoints, stale metadata,
and gradients. Application chunks must contain no data requests or browser
persistence. Generated artifacts remain ignored by Git.

Upload **only the contents of `dashboard/out/`**, either as a folder or as a ZIP
whose root contains `index.html` and `vercel.json`, using
[Vercel Drop](https://vercel.com/drop). Select the existing Hobby workspace and
the project name `llm-eval-control-plane-idevelopai`. Do not upload the repository
root, `.next/`, `.env` files, backend source, or local evidence. Do not select a
framework, build command, Pro trial, runtime secret, integration, or paid add-on.

In Project Settings → General → Vercel Toolbar, set both production and
pre-production overrides to **Off**. Apply this before deployment when possible;
otherwise redeploy the same source with the updated settings. This avoids
Vercel appending a toolbar loader to an otherwise byte-identical static asset.
Leave Web Analytics and Speed Insights disabled.

The new-project Drop flow creates a project; it does not set up automatic Git
deployments. Do not repeat that flow for updates. Use an upload explicitly scoped
to the existing project, Vercel's authenticated CLI, or a deliberately connected
reviewed Git repository, preserving the static-only build and upload boundary.
The deployment's **Redeploy** action reuses the same source with current project
settings; it does not upload changed local files.

## Cost and access boundary

Use [Vercel Hobby](https://vercel.com/docs/plans/hobby) only for eligible personal,
non-commercial use. Static hosting still consumes Vercel request and transfer
allowances; it is not unlimited. Reaching free-plan limits can make the demo
unavailable until the limit resets. This configuration provisions no billable
add-on and does not upgrade the account. It requires no custom domain purchase.

Visitors download static files and interact with preloaded synthetic evidence.
There is no ChatGPT, Codex, model-provider, or control-plane request in that
interaction path. Development and deployment tooling are separate from visits.

## Verification and rollback

Before changing the public README link, verify the assigned production URL:

- It is reachable without cookies or a sign-in challenge.
- Canonical and social metadata use the assigned Vercel origin, and `noindex`
  remains enabled.
- The existing CSP, referrer, frame, MIME, permissions, and cross-origin headers
  are present. Root HTML remains `private, no-store, max-age=0`.
- `/api` and `/v1` paths return 404 for reads and writes; other writes are denied.
  Unknown paths return 404, not a successful dashboard fallback.
- Check `OPTIONS` separately: Vercel can synthesize an empty 204 for static
  responses despite the configured 404/405. It must not expose a backend, return
  application data, or grant cross-origin preflight permission. An empty 204 is
  not evidence of an available API.
- Assets load, gate selection and filters work, and no local credential controls
  appear. The deployment has static assets only, with no server functions.
- Compare every public file with the local verified export, including JavaScript;
  a host-injected toolbar must not silently bypass the artifact checks.

Keep the preceding Site available until this verification succeeds. If the
Vercel deployment fails its boundary checks, do not cut over the README link;
retain the previous deployment and repair the artifact. The historical release
record and existing Sites-compatible build are preserved, not rewritten as a
Vercel deployment record.

## Release record

Verified on **2026-09-25**:

- Production URL: <https://llm-eval-control-plane-idevelopai.vercel.app/>.
- Source revision: `8e5b118` (the release-record update is documentation only).
- Final deployment: `dpl_8GfuW1wNs9LmEkWPy2561GbaceHm`, Ready / Production.
- Uploaded ZIP SHA-256:
  `c31eb1460f779b5dac64826fb651510acf31e3d0103e28a4e810cc57e7f90c54`.
- Artifact: 59 files / 2,385,050 bytes, including the host configuration.
  Vercel reports 58 static assets and no deployed functions.
- All 58 public files matched the local export byte-for-byte after disabling
  the toolbar and redeploying. Canonical/social metadata and `noindex` matched.
- 49 unauthenticated method/path checks passed with the expected security
  headers. Reads and writes to `/api`, `/api/probe`, `/v1`, and `/v1/probe`
  returned 404. Root/index writes returned 405; unknown reads returned 404.
  The observed `OPTIONS` exceptions were empty 204 responses on `/`,
  `/index.html`, `/v1`, and `/v1/probe`, without an
  `Access-Control-Allow-Origin` header.
- Root HTML retained `private, no-store, max-age=0` and set no cookie.
  Language/task filters, the answerability empty state, failed-gate navigation,
  case expansion, and bounded distribution updates were verified in-browser.
- Hobby workspace retained; no paid add-ons, model-provider credentials,
  database, Git integration, Web Analytics, or Speed Insights were configured.
- Local checks passed: 473 dashboard tests, 6 static-boundary tests, lint,
  typecheck, both production builds and their verifiers, and the existing public
  response smoke test. A full-history redacted secret scan found no leaks.

The preceding Site remains available as a fallback. This record does not claim
that the old host has been retired.
