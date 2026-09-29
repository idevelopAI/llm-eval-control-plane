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

The check runs the default `pnpm run build`, generating a native Next.js static
export in `out/` plus its reviewed `vercel.json`, followed by static-preview and
local-development smoke tests. `build:vercel` is an alias for the same build.
The artifact verifier requires static routes, no proxy rewrites or server
actions, only the public fixture client entry, and the exact CDN policy.
It rejects symlinks, unexpected file types, source maps,
credential markers, local write controls, provider endpoints, stale metadata,
and gradients. Application chunks must contain no data requests or browser
persistence. Generated artifacts remain ignored by Git.

Upload **only the contents of `dashboard/out/`** to the existing Hobby project
`llm-eval-control-plane-idevelopai`. With the authenticated Vercel CLI, run from
`dashboard/`, replacing `YOUR_EXISTING_HOBBY_WORKSPACE` with the project's actual
workspace slug:

```bash
# Inspect the upload boundary without creating a deployment.
vercel deploy --cwd out --project llm-eval-control-plane-idevelopai \
  --scope YOUR_EXISTING_HOBBY_WORKSPACE --dry --json

# Publish only after reviewing the dry run and passing the checks above.
vercel deploy --cwd out --project llm-eval-control-plane-idevelopai \
  --scope YOUR_EXISTING_HOBBY_WORKSPACE --prod
```

The dry run must identify the `Other` framework and list only the verified static
export and `vercel.json`. CLI authentication stays outside the artifact. Do not
upload the repository root, `.next/`, `.env` files, backend source, or local
evidence. Do not select a framework, build command, Pro trial, runtime secret,
integration, or paid add-on. Do not use `--prebuilt`: a Next.js `out/` directory
is a static export, not Vercel's separate Build Output API format.

In Project Settings → General → Vercel Toolbar, set both production and
pre-production overrides to **Off**. Apply this before deployment when possible;
otherwise redeploy the same source with the updated settings. This avoids
Vercel appending a toolbar loader to an otherwise byte-identical static asset.
Leave Web Analytics and Speed Insights disabled.

The [new-project Drop flow](https://vercel.com/drop) creates a project; it does
not set up automatic Git deployments. Do not repeat that flow for updates. An
alternative upload must be explicitly scoped to the existing project, with
`index.html` and `vercel.json` at its root, preserving the static-only boundary.
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
- Vercel may add `Access-Control-Allow-Origin: *` to public static HTML and
  static error responses. Verify that those bodies still match the public
  export, that preflight responses do not grant permission, and that no response
  sets `Access-Control-Allow-Credentials`, `Access-Control-Allow-Methods`, or
  `Access-Control-Allow-Headers`. Do not confuse this CDN behavior with the
  local preview, which emits no CORS allowance.
- Assets load, gate selection and filters work, and no local credential controls
  appear. The deployment has static assets only, with no server functions.
- At phone, tablet, and laptop widths, check that every gate's scores, delta,
  coverage, and outcome remain inside its card. The compact layout follows the
  ledger's available width, not just the viewport width.
- Compare every public file with the local verified export, including JavaScript;
  a host-injected toolbar must not silently bypass the artifact checks.

If a candidate fails these checks, retain the last verified Vercel deployment
and repair the artifact. Roll back an unsafe deployment to the preceding verified
Vercel version, or withdraw public access if no safe version is available, then
repeat artifact and live-origin verification. The old host is not a public
fallback, and its retired build path is no longer maintained. See
[ADR 0015](../adr/0015-native-static-vercel-hosting.md).

## Release record

### Current release — 2026-09-29

- Production URL: <https://llm-eval-control-plane-idevelopai.vercel.app/>.
- Dashboard source revision: `5fbfc0b02856a12f87dc4fe960f9ecb561ac6ab0`.
  Subsequent README and release-record changes do not alter the export.
- Deployment: `dpl_C9uF75ButNjLpm7d2baCDQSbUYKK`, Ready / Production.
  Published with Vercel CLI 60.1.3 to the existing Hobby project, uploading only
  the reviewed static export after an upload dry run.
- Artifact: 59 regular files / 2,384,965 bytes, including `vercel.json`.
  Vercel serves 58 static assets; no server functions are deployed.
- All 58 public files matched the local export byte-for-byte, including CSS,
  JavaScript, HTML, and images. Public-file manifest SHA-256:
  `3f76b6ac19faf3b34a979b9f86a721e6cd1d2fc92307c361da6a6df95f2aa6a1`.
  This hashes sorted records of `path`, a NUL separator, the file's SHA-256,
  and a newline; the host-only `vercel.json` is excluded.
- 53 unauthenticated method/path checks passed. `/api`, `/api/probe`, `/v1`,
  and `/v1/probe` denied reads and writes with 404; root/index writes returned
  405. Unknown reads and `/.env`, `/.git/config`, `/package.json`, and
  `/vercel.json` returned 404. Security headers, canonical/social metadata,
  `noindex`, and private/no-store HTML caching matched the expected policy.
- The four empty-204 `OPTIONS` exceptions remained `/`, `/index.html`, `/v1`,
  and `/v1/probe`. Synthetic preflights requested `POST` with authorization and
  content-type headers; no response granted preflight permission. Some public
  HTML and error responses carried the CDN's wildcard origin header, but their
  bodies matched the public export. No credential allowance, allowed-method or
  allowed-header grant, or cookie was present.
- Gate filters, the empty state, failed-gate navigation, score expansion, and
  bounded distributions worked in the browser. A laptop-width overflow found
  during verification was fixed by sizing the compact gate layout against its
  panel. All four gate rows fit at 320, 390, 768, 1024, 1280, 1440, and 1920px
  viewport widths in the local production export; the corrected layout was
  also visually verified on the public deployment.
- Local validation passed: 294 tests across 23 files, 6 static-boundary tests,
  generated API types, lint, typecheck, native static build, artifact verifier,
  public-preview smoke test, and loopback-only development smoke test.
  The locked dependency audit reported no known vulnerabilities.
- No account upgrade, paid add-on, backend, database, runtime credential, or
  model-provider integration was introduced. Toolbar injection remains off;
  Web Analytics and Speed Insights remain disabled. Static delivery and
  deployment still consume the existing Hobby allowances.

### Previous release — 2026-09-25

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

### Former host access withdrawal

On **2026-09-25**, after the Vercel checks above, the former Site was changed to
owner-only access with explicit approval. It had no additional allowed users or
groups, and an anonymous request returned 401. It remains privately recoverable;
it was not deleted. The [former release record](public-site-release.md) describes
historical evidence only and is not an active deployment or rollback guide.

### Source cleanup

The current source uses only native Next.js development and static export.
Legacy hosting metadata, plugin dependencies, Worker build configuration, and
the unused platform-authenticated read prototype have been removed. The
September 29 release deploys this cleaned source and the subsequent dependency
and responsive-layout updates. The September 25 record is retained as historical
evidence, not the current artifact. Neither the cleanup nor the new Vercel
deployment changes the former host's owner-only access setting.
