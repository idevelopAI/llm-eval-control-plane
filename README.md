# LLM Eval Control Plane

[![CI](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml)

A deterministic-first control plane for evaluating AI applications, comparing
candidate and baseline versions, and turning quality, safety, latency, and cost
requirements into release gates.

> **Status:** Foundation milestone. The repository currently defines the core
> contracts and development workflow; evaluation execution is not implemented
> yet.

## Current capabilities

- Immutable references for datasets, targets, prompts, evaluators, suites, and
  gate policies
- Versioned evaluation specifications with deterministic metric thresholds and
  regression budgets
- Strict validation that rejects unknown fields, invalid artifact roles,
  duplicate gates, non-finite thresholds, and self-comparisons
- JSON Schema inspection and local specification validation through `llm-eval`
- Python 3.11–3.14 support target with locked dependencies and typed source

## Why this project exists

AI application changes can improve an average score while quietly breaking a
language, safety category, or refusal path. LLM Eval Control Plane is designed
to make those regressions reproducible and visible before release.

The first integrations will evaluate:

- bilingual text-to-SQL correctness and safety in DataBridge AI;
- retrieval, citation, groundedness, and refusal quality in a RAG knowledge base.

## Design principles

- Prefer deterministic evaluators before introducing model-based judges.
- Version every dataset, target, evaluator, suite, and gate policy.
- Preserve case-level evidence behind every aggregate metric.
- Treat safety regressions as blocking independently of average quality.
- Keep offline CI fixtures deterministic and separate from live-provider runs.
- Never collect prompts, answers, SQL rows, or document text in telemetry by
  default.

## Development

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

Inspect the current evaluation-specification contract or validate a JSON file:

```bash
uv run llm-eval schema
uv run llm-eval validate examples/evaluation-spec.json
```

The CLI deliberately does not execute evaluations yet. Execution, persistence,
and comparison will be added as complete vertical slices in later milestones.

## Architecture

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [Architecture decisions](docs/adr/)

The project starts as a modular monolith. Its dependency direction is
`entrypoints/adapters → application → domain`; the domain never imports web,
database, queue, telemetry, or provider SDKs.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting and the evaluation-data
handling policy.

## License

Licensed under the [MIT License](LICENSE).
