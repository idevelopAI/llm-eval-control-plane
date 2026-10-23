# Contributing

## Development setup

Install Python 3.11 or newer and
[uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv sync --locked
```

For the dashboard, use Node.js 22.22.2+ on the 22.x line, 24.15.0+ on the
24.x line, or 26+. CI currently pins Node.js 24.19.0. Use the package manager
declared in `dashboard/package.json` (pnpm 11.19.0), then run
`pnpm install --frozen-lockfile` and `pnpm run check` from `dashboard/`.
The minimum Node versions also apply to the jsdom-backed test environment.

## Quality checks

Run these database-free checks from the repository root:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run ruff check --select S src scripts migrations
uv run mypy src tests scripts migrations
uv run pytest -q tests/test_suite_cli.py tests/test_release_workflow.py tests/test_offline_workflow.py
uv sync --locked --group security --no-install-project
uv run --no-sync pip-audit \
  --local --strict --skip-editable --progress-spinner off --desc off
uv build
```

For the full Python test and coverage command, follow the
[disposable PostgreSQL test setup](docs/operations/local-tests.md). It supplies
`CONTROL_PLANE_DATABASE_URL` and applies Alembic migrations before pytest, as
the CI Python matrix does. Running pytest without that setup skips the
control-plane PostgreSQL integration tests; an offline pass is not equivalent
to the database-backed CI run. Never point integration tests at an existing
development or production database.

DataBridge replay uses a separate restricted-role fixture and
`DATABRIDGE_EVAL_DSN`; the test setup explains that separate gate and its expected
skips. Docker Compose is also required for the deployment-configuration tests.

The hosted security gate also performs a checksum-verified, fully redacted scan
of every reachable commit, scans the built runtime image and deployment
configuration, and runs CodeQL's `security-extended` queries. Its exact required
check names are `Dependency Vulnerability Audit`, `Static Security Analysis`,
`Secret History Scan`, `Container Security Gate`, and `CodeQL Python`.

## Change discipline

- Keep each change focused and include tests with behavior changes.
- Preserve the dependency direction documented in `docs/architecture.md`.
- Add a decision record when changing a public contract or system boundary.
- Do not commit secrets, private prompts, proprietary documents, SQL rows, or
  provider responses containing personal data.
- Authentication examples must be deliberately non-usable placeholders. Never
  commit a bearer value; runtime configuration stores only its SHA-256 digest.
- Treat one deployment and database as one project. Do not imply row-level
  multitenancy or weaken the exact `X-Project-ID` and scope checks.
- New logs, metrics, spans, or links need bounded allowlists and tests proving
  that request content, evaluation evidence, identity, credentials, coordination
  metadata, raw cursors, and exception text cannot escape.
- Review Action source and nested composite actions before updating a full-SHA
  pin. Review image provenance before changing a manifest digest.
- Update the threat model and recovery runbook when a change adds an asset,
  trust boundary, credential type, telemetry field, supply-chain input, or
  recovery step.
- Do not publish benchmark numbers that cannot be reproduced from a tagged
  dataset and configuration.

## Dependency updates

Dashboard dependency updates follow compatibility boundaries. React, React DOM,
and their types update together across production and
development dependencies. Next.js and its ESLint configuration form a second
group. Vite (used by Vitest, not the hosting build) has its own minor/patch
group; other development dependencies use a separate minor/patch group.
Major tooling updates, including Vitest, remain individual pull requests rather
than joining either batch. Existing explicit major-version exclusions remain
unchanged, and security updates retain Dependabot's separate handling.

Before merging an update, refresh it against the current base and require fresh
checks. Dashboard updates must pass a frozen-lockfile install, the vulnerability
audit, and `pnpm run check` (including the static export, artifact verification,
static-preview probes, and native development proxy smoke test). Keep the
transitive security floors in `pnpm-workspace.yaml`; do not
silence an audit or bypass typing to make a dependency update pass. A grouped
update that fails should be isolated before widening the batch.

## Pull requests

Describe the problem, the chosen trade-offs, and the validation performed.
Changes to evaluators must explain their failure modes and provide deterministic
fixtures whenever possible. Security-sensitive changes must state residual
risk, the least privilege granted, the safe failure behavior, and the privacy or
recovery tests that cover the boundary.
