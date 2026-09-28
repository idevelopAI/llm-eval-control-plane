# Offline evaluation reference

Run these commands from the repository root. The examples below use the
original, suite-unpinned CLI. For new workflows, start with the [offline suite
guide](suite-cli.md), which pins the dataset, evaluators, execution contract, and
release policy together. Both workflows run without Docker, a database, or
provider credentials. Dependency installation may require network access; the
evaluations themselves do not.

## Schema and validation

Inspect the original evaluation-specification contract or validate an authored
specification without executing a target:

```bash
uv run llm-eval schema
uv run llm-eval validate examples/evaluation-spec.json
```

## Baseline comparison and release gates

The release fixture contains 40 English and German quality/refusal cases. Run
the baseline and a deliberately regressed candidate with only deterministic
scorers:

```bash
uv sync --locked
uv run llm-eval run examples/release-gate-40.jsonl \
  --run-id baseline-v1 \
  --dataset-name release-gate/offline \
  --target-name fake/release \
  --target-revision 1 \
  --scorer exact_match --scorer refusal --scorer latency

uv run llm-eval run examples/release-gate-40.jsonl \
  --run-id candidate-v2-regression \
  --dataset-name release-gate/offline \
  --target-name fake/release \
  --target-revision 2 \
  --scenario-overrides examples/release-regression-overrides.json \
  --scorer exact_match --scorer refusal --scorer latency

uv run llm-eval compare \
  examples/release-gate-spec.json \
  examples/release-gate-40.jsonl \
  --baseline-run baseline-v1 \
  --candidate-run candidate-v2-regression \
  --format markdown
```

The final command intentionally returns `1`: broad quality remains inside its
budget while the refusal-only safety slice catches a regression.

| Gate | Baseline | Candidate | Delta | Decision |
|---|---:|---:|---:|---|
| Exact match, all 40 cases | `1.0` | `0.95` | `-0.05` | Pass |
| Exact match, `language/de` | `1.0` | `0.95` | `-0.05` | Pass |
| Refusal correctness, `safety/refusal` | `1.0` | `0.875` | `-0.125` | **Fail** |
| Simulated latency, all cases | `5.0 ms` | `5.0 ms` | `0.0 ms` | Pass |

`delta` always means `candidate - baseline`. `allowed_regression` is an
absolute budget in metric units. Every gate also requires matching scored and
skipped coverage with no execution errors, so a technical failure cannot be
mistaken for a good score.

Reports support `--format json`, `--format markdown`, and `--format junit`.
Use `--output PATH` to create a new report file; existing files are never
overwritten. Reports include artifact identities, metrics, slice names, and case
IDs, but omit case inputs, expected values, and target outputs.

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
or integrity errors. The `compare` command returns `0` for a passing release,
`1` for a valid failed release decision, and `2` when comparison could not be
performed safely.
