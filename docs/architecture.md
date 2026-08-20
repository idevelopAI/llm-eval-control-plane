# Architecture

## System objective

LLM Eval Control Plane turns AI application behavior into reproducible evidence.
The implemented Phase 2 slice loads a content-addressed dataset, invokes a
versioned target once per case, validates untrusted responses, applies versioned
evaluators, preserves case-level outcomes, aggregates coverage-aware metrics,
and atomically stores a complete immutable run. It then aligns candidate and
baseline evidence, recomputes global and slice aggregates, applies release
policy, and renders a content-addressed decision.

## Architectural style

The project is a modular monolith. The CLI is the composition root: it constructs
concrete adapters and passes them into application-owned protocol ports.

```mermaid
flowchart LR
    CLI["CLI composition root"] --> RUNNER["Application runner"]
    CLI --> COMPARE["Comparison + gate service"]
    CLI --> ADAPTERS["Concrete adapters"]
    RUNNER --> PORTS["Target / evaluator / repository ports"]
    RUNNER --> DOMAIN["Immutable domain contracts"]
    COMPARE --> DOMAIN
    ADAPTERS -. implement .-> PORTS
    ADAPTERS --> DOMAIN
```

The compile-time dependency rule is:

```text
entrypoints and adapters -> application -> domain
```

The application layer does not import concrete adapters. The domain does not
import CLI, persistence, network, telemetry, queue, database, or provider SDKs.

## Implemented structure

```text
src/llm_eval_control_plane/
├── cli.py
├── application/
│   ├── ports.py           # target, evaluator, and run-repository protocols
│   ├── runner.py          # serial in-process orchestration and aggregation
│   └── comparison.py      # alignment, slice aggregation, and gate decisions
├── adapters/
│   ├── fake_target.py     # deterministic offline target and synthetic clock
│   ├── filesystem.py      # atomic append-only local run storage
│   ├── jsonl.py           # strict normalized dataset transport
│   ├── reports.py         # safe JSON, Markdown, and JUnit release evidence
│   └── scorers.py         # deterministic built-in evaluators
└── domain/
    ├── artifacts.py       # immutable version references
    ├── canonical.py       # strict parsing and RFC 8785 hashing
    ├── datasets.py        # reviewed cases and dataset versions
    ├── comparison.py      # release decision evidence and content digest
    ├── evaluation.py      # slice-aware release policy
    ├── execution.py       # target and evaluator result envelopes
    ├── models.py          # shared strict/frozen model behavior
    └── results.py         # case evidence, aggregates, and run digests
```

## Evaluation and release flow

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Loader as JSONL adapter
    participant Runner as Application runner
    participant Target as Target port
    participant Eval as Evaluator ports
    participant Store as Run repository

    User->>CLI: run dataset + immutable run ID
    CLI->>Loader: parse strict UTF-8 JSONL
    Loader-->>CLI: sorted, content-addressed dataset
    CLI->>Runner: inject target, evaluators, clock
    loop Every case in canonical order
        Runner->>Target: case ID + input only
        Target-->>Runner: untrusted response envelope
        Runner->>Runner: validate response and measured latency
        Runner->>Eval: case expectations + validated observation
        Eval-->>Runner: scored / skipped / error evidence
    end
    Runner->>Runner: aggregate every attempted metric
    Runner-->>CLI: immutable RunResult + result digest
    CLI->>Store: atomic create-once save
    CLI-->>User: redacted JSON summary
```

After both runs exist, comparison follows a separate read-only application
path:

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Store as Run repository
    participant Compare as Comparison service
    participant Report as Report adapter

    User->>CLI: compare policy + dataset + two run IDs
    CLI->>Store: load baseline and candidate
    CLI->>Compare: policy + resolved dataset + run evidence
    Compare->>Compare: verify artifacts, cases, evaluators, stored summaries
    Compare->>Compare: recompute every metric globally and per slice
    Compare->>Compare: apply coverage, threshold, and regression checks
    Compare-->>CLI: content-addressed ReleaseDecision
    CLI->>Report: JSON / Markdown / JUnit
    Report-->>User: redacted decision + exit 0 or 1
```

Comparison never invokes a target or evaluator. A decision is produced only
when both runs use the exact supplied dataset, have identical case and metric
sets, match their policy target revisions, and contain stored global summaries
that agree with recomputed case evidence.

Target expectations are never passed through the target port. Target and
evaluator exceptions are converted to bounded failure codes; remaining cases
continue. The runner catches ordinary exceptions but does not swallow process
control exceptions such as cancellation or keyboard interruption.

## Determinism boundaries

- Dataset content uses RFC 8785 canonical JSON. Case and slice order are
  normalized before hashing.
- Result arrays have enforced canonical order. The result digest excludes the
  caller-selected run ID but includes outputs, observations, usage, and measured
  latency.
- The offline CLI injects a fixed-step clock so the checked-in fixture produces
  stable bytes. Its 5 ms values are synthetic and must not be presented as a
  performance benchmark.
- A future live target will use the runner's monotonic clock; measured latency
  will then intentionally change the result digest.
- Aggregate and case deltas use `candidate - baseline`. Gate boundary checks use
  a fixed `1e-12` absolute numeric tolerance solely for machine-precision noise.
- A release-decision digest covers resolved artifact and result digests,
  aggregates, gates, and case transitions. It excludes human-selected run IDs.

## Persistence contract

One complete run is stored as an RFC 8785 envelope plus exactly one LF. Run IDs
are validated before path construction and mapped to domain-separated SHA-256
filenames, avoiding traversal, reserved-name, and case-insensitive collisions.

Publishing uses a fully written same-directory temporary file and an atomic hard
link. Existing byte-identical content is an idempotent success; different valid
content is a conflict; corrupt or special files fail closed. Reads are bounded,
reject symlinks and non-regular files where the platform supports those checks,
revalidate the storage schema and domain digest, and require exact canonical
bytes.

## Trust boundaries

- Dataset lines, target outputs, schemas, and stored bytes are untrusted.
- Remote JSON Schema references are disabled; evaluation never performs schema
  network fetches.
- Credentials are absent from Phase 2. The offline target requires no environment
  variables, keys, or provider SDKs.
- Default CLI output contains bounded identifiers, digests, counts, metrics, and
  failure codes—not inputs, expectations, target outputs, or exception text.
- Local artifacts can contain evaluation content. `.llm-eval/` is ignored, and
  POSIX stores use `0700` directories and `0600` files.
- Default release reports contain metrics and case IDs but never inputs,
  expectations, outputs, exception text, or absolute storage paths.

## Deferred boundaries

Real target adapters, PostgreSQL, durable queues, an HTTP API, a dashboard,
OpenTelemetry, authentication, and cloud infrastructure will be introduced only
with a complete vertical slice and its tests. Kubernetes, multi-cloud
abstractions, billing, and arbitrary third-party Python plugins are outside the
MVP.
