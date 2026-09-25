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

Vercel Drop creates a new project per upload; it does not set up automatic Git
deployments. For a later update to this project, use Vercel's authenticated CLI
or deliberately connect the reviewed Git repository, preserving the static-only
build and upload boundary. Do not create replacement projects accidentally.

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
- Assets load, gate selection and filters work, and no local credential controls
  appear. The deployment has static assets only, with no server functions.

Keep the preceding Site available until this verification succeeds. If the
Vercel deployment fails its boundary checks, do not cut over the README link;
retain the previous deployment and repair the artifact. The historical release
record and existing Sites-compatible build are preserved, not rewritten as a
Vercel deployment record.

## Release record

Publication verification is pending. The intended canonical origin is
`https://llm-eval-control-plane-idevelopai.vercel.app/`; availability must be
confirmed in Vercel before acceptance.
