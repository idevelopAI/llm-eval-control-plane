# LLM Eval Control Plane

[![CI](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/idevelopAI/llm-eval-control-plane/actions/workflows/ci.yml)

A deterministic-first control plane for evaluating AI application behavior,
preserving case-level evidence, and making quality, safety, latency, and usage
changes measurable before release.

> **Status:** Phase 1 evaluation spine. The repository runs reviewed JSONL
> datasets end to end against a deterministic offline target, applies versioned
> scorers, aggregates coverage-aware metrics, and stores immutable local run
> evidence. Real-provider targets, candidate/baseline comparison, and release
> gate execution are not implemented yet.

## Reproducible 100-case demo

The reference workflow evaluates 100 synthetic cases without network access,
credentials, paid APIs, or model-provider dependencies:

```bash
uv sync --locked
uv run llm-eval run examples/offline-100.jsonl \
  --run-id offline-100-v1 \
  --dataset-name offline-100 \
  --dataset-revision 1
```

The committed fixture deliberately mixes exact text, uppercase transformation,
numeric tolerance, structured refusal, and JSON-schema cases. Its golden run
produces:

| Metric | Mean | Scored | Skipped | Errors |
|---|---:|---:|---:|---:|
| Exact match | `0.95` | 100 | 0 | 0 |
| JSON-schema validity | `1.0` | 10 | 90 | 0 |
| Numeric tolerance | `1.0` | 5 | 95 | 0 |
| Structured refusal correctness | `1.0` | 100 | 0 | 0 |
| Simulated latency | `5.0 ms` | 100 | 0 | 0 |

The dataset digest is
`sha256:83296a96077826f7523365b6db509e06ebe056297fcba1b4203e59f63a4852f0`.
The stable result-content digest is
`sha256:2544034c0247bd53c52b044496791d3e1b800c8153538b7db14885562cad3f58`.
Both are pinned in integration tests.

The offline clock advances by a fixed 5 ms so the run artifact is reproducible.
That latency value is synthetic and is not a performance benchmark. Usage values
are deterministic fixture estimates, not provider token counts or cost claims.

## Inspecting evidence safely

Run summaries include artifact identities, counts, aggregate metrics, and
digests. Case inputs, expected values, and target outputs are omitted:

```bash
uv run llm-eval show offline-100-v1
uv run llm-eval show offline-100-v1 --case offline-001
```

Target output is disclosed only when one case is selected explicitly:

```bash
uv run llm-eval show offline-100-v1 \
  --case offline-001 \
  --include-output
```

Complete evidence is stored under `.llm-eval/` in canonical, integrity-checked,
append-only files. That directory is ignored by Git because artifacts can contain
model inputs and outputs. On POSIX systems, the store uses owner-only directory
and file permissions.

The `run` command returns `0` when execution completes, `1` when sanitized target
or evaluator failures were persisted, and `2` for input, configuration, storage,
or integrity errors. A zero metric score is evidence, not a release decision;
release gates are a later vertical slice.

## Current capabilities

- RFC 8785 canonical JSON with duplicate-key, non-finite-number, and malformed
  input rejection
- Content-addressed datasets whose identity is independent of JSONL authoring
  order and dataset labels
- Deterministic exact, normalized-text, JSON-schema, numeric-tolerance, refusal,
  latency, and usage scorers
- One target invocation per case, explicit scored/skipped/error observations,
  sanitized failures, and coverage-aware aggregates
- Atomic create-once local persistence with hashed storage keys, bounded reads,
  canonical-byte validation, and digest verification
- Safe JSON CLI summaries plus opt-in per-case output disclosure
- Python 3.11–3.14 CI, strict typing, linting, branch coverage, packaging, and
  isolated wheel smoke tests

## Design principles

- Prefer deterministic evaluators before introducing model-based judges.
- Version and hash every reproducibility-relevant artifact.
- Preserve case-level evidence behind every aggregate metric.
- Count skipped and failed evaluations instead of silently dropping coverage.
- Keep offline CI fixtures deterministic and separate from live-provider runs.
- Never expose prompts, expected values, outputs, or private exception details in
  default CLI output or telemetry.

## Development

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --locked
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --cov=llm_eval_control_plane --cov-branch
uv build
```

The original evaluation-specification contracts remain available:

```bash
uv run llm-eval schema
uv run llm-eval validate examples/evaluation-spec.json
```

## Architecture

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [Architecture decisions](docs/adr/)

The project is a modular monolith with dependency direction
`entrypoints/adapters → application → domain`. The CLI is the composition root;
the application layer depends on target, evaluator, and repository protocols,
not concrete adapters.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting and the evaluation-data
handling policy.

## License

Licensed under the [MIT License](LICENSE).
