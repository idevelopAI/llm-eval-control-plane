# LLM Eval Control Plane

A deterministic-first control plane for evaluating AI applications, comparing
candidate and baseline versions, and turning quality, safety, latency, and cost
requirements into release gates.

> **Status:** Foundation milestone. The repository currently defines the core
> contracts and development workflow; evaluation execution is not implemented
> yet.

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

The command-line entry point, detailed architecture, and evaluation workflow
will be added incrementally in the next milestones.

## License

Licensed under the [MIT License](LICENSE).
