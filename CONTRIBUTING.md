# Contributing

## Development setup

Install Python 3.11 or newer and
[uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv sync --locked
```

## Quality checks

Run the same checks used by continuous integration:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run ruff check --select S src scripts migrations
uv run mypy src tests scripts migrations
uv run pytest --cov=llm_eval_control_plane --cov-branch --cov-report=term-missing
uv run pytest -q tests/security/test_deployment_hardening.py
uv sync --locked --group security --no-install-project
uv run --no-sync pip-audit \
  --local --strict --skip-editable --progress-spinner off --desc off
uv build
```

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

## Pull requests

Describe the problem, the chosen trade-offs, and the validation performed.
Changes to evaluators must explain their failure modes and provide deterministic
fixtures whenever possible. Security-sensitive changes must state residual
risk, the least privilege granted, the safe failure behavior, and the privacy or
recovery tests that cover the boundary.
