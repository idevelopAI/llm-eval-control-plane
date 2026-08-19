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
uv run mypy src tests
uv run pytest --cov=llm_eval_control_plane --cov-branch --cov-report=term-missing
uv build
```

## Change discipline

- Keep each change focused and include tests with behavior changes.
- Preserve the dependency direction documented in `docs/architecture.md`.
- Add a decision record when changing a public contract or system boundary.
- Do not commit secrets, private prompts, proprietary documents, SQL rows, or
  provider responses containing personal data.
- Do not publish benchmark numbers that cannot be reproduced from a tagged
  dataset and configuration.

## Pull requests

Describe the problem, the chosen trade-offs, and the validation performed.
Changes to evaluators must explain their failure modes and provide deterministic
fixtures whenever possible.
