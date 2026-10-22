# Single-root Next.js lint patch

The dashboard uses one ESLint working directory (`dashboard/`), not Next.js's
optional `settings.next.rootDir` monorepo discovery. The exact-version patch for
`@next/eslint-plugin-next@16.4.2` removes that unused glob-discovery code and its
only `fast-glob` import. The scoped override removes the now-unused dependency,
including its `micromatch > braces` chain, from the installed graph.

This remediates this repository's exposure to
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm) by removing
the affected package. It does not patch `braces` itself or claim an upstream fix.
No advisory is ignored, no audit threshold is lowered, and no Next.js lint rule
is removed or disabled. Default root discovery still returns `context.cwd`.
Any defined `settings.next.rootDir`, including strings and arrays, now throws a
clear configuration error instead of silently omitting checks.

`pnpm run test:lint-toolchain` verifies the exact plugin version, the unchanged
default root, rejection of custom/deeply nested patterns, absence of the removed
dependency chain, all recommended Next rules, and actual internal-link lint
diagnostics for App Router and Pages Router fixtures. It runs in `pnpm run check`.
The patch hash is recorded in `pnpm-lock.yaml`; pnpm rejects missing/unapplied
exact-version patches by default. Do not enable `allowUnusedPatches`.

## Updating or removing the patch

For the next Next.js/ESLint upgrade, review the upstream dependency graph first.
Once a supported upstream version removes the advisory, remove the patch entry,
the matching `fast-glob` override, and the patch file together. Update the
exact-version/custom-root regression tests to match the reviewed upstream
behavior while retaining the lint-diagnostic checks. Regenerate the lockfile,
perform a fresh frozen installation, and run the full audit and dashboard checks.

If the upstream chain is still affected, explicitly review and port this small
patch to the new exact plugin version. Never broaden the patch or override to
all future versions merely to make an upgrade install. Supporting a monorepo
or custom roots also requires a separate reviewed change; do not remove the
configuration error while retaining an incomplete root-discovery implementation.
