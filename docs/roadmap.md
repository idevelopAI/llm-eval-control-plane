# Project Roadmap

The roadmap is organized as vertical slices. Every phase must end in runnable,
tested behavior rather than placeholder infrastructure.

## Phase 0 — Foundation

- [x] Initialize a typed Python package with locked dependencies.
- [x] Define immutable artifact references and evaluation specifications.
- [x] Define directional metric gates and validation invariants.
- [x] Add CLI schema inspection and specification validation.
- [x] Add contract tests with branch coverage enforcement.
- [x] Document the architecture, domain vocabulary, security policy, and roadmap.
- [ ] Publish the repository and enable continuous integration.

### Definition of done

- A clean checkout can install from `uv.lock`.
- Formatting, linting, strict typing, tests, coverage, and package build pass.
- The example specification validates through the public CLI.
- Architecture documentation clearly separates implemented and planned behavior.

## Phase 1 — Deterministic evaluation spine

- [ ] Model immutable evaluation cases and dataset versions.
- [ ] Compute canonical dataset digests with documented serialization rules.
- [ ] Define target request/result envelopes without provider-specific fields.
- [ ] Define evaluator and target `Protocol` ports.
- [ ] Implement exact-match, normalized-match, JSON-schema, numeric-tolerance,
      refusal, latency, and usage scorers.
- [ ] Add an in-process runner before introducing a queue.
- [ ] Add a deterministic fake target with intentional pass/fail cases.
- [ ] Import and export reviewed datasets as JSONL.
- [ ] Persist run artifacts to a local filesystem repository for the first slice.
- [ ] Add CLI commands to run a suite and inspect case-level results.

### Definition of done

- One command runs a 100-case offline fixture suite end to end.
- Repeated identical inputs produce identical dataset digests and metric output.
- Invalid target output becomes a structured case failure.
- Core evaluation code maintains at least 90% branch coverage.

## Phase 2 — Baseline comparison and release gates

- [ ] Compare candidate and baseline runs case by case.
- [ ] Calculate absolute deltas and clearly defined regression budgets.
- [ ] Identify newly passing, newly failing, and unchanged cases.
- [ ] Aggregate results by language, task type, answerability, and safety slice.
- [ ] Add zero-regression safety gates independent of average quality.
- [ ] Produce JSON, Markdown, and JUnit reports.
- [ ] Return stable CLI exit codes for passed, failed, and invalid runs.
- [ ] Add a GitHub Action that blocks an intentional fixture regression.

### Definition of done

- A pull request receives a reproducible pass/fail decision.
- The report identifies the exact metrics, slices, and cases behind a failure.
- Identical runs produce zero deltas and pass.
- A seeded safety regression is blocked even if average task quality improves.

## Phase 3 — DataBridge AI adapter

- [ ] Define a versioned HTTP/JSON adapter contract for DataBridge AI.
- [ ] Support deterministic mock mode and explicitly configured live mode.
- [ ] Import bilingual, ambiguous, and adversarial SQL cases.
- [ ] Add SQL parse validity and read-only policy scorers.
- [ ] Add result-set and expected-column equivalence scorers.
- [ ] Add refusal and clarification correctness scorers.
- [ ] Slice results by language, query type, ambiguity, and safety category.
- [ ] Execute SQL evidence only against an isolated disposable database.
- [ ] Prove unsafe cases cannot mutate the fixture database.

### Definition of done

- CI evaluates the DataBridge-compatible mock without a model API key.
- Live mode reports real accuracy, rejection, latency, and usage separately.
- Every failed SQL case includes sanitized parse, policy, or execution evidence.
- All mutation attempts leave the fixture database unchanged.

## Phase 4 — RAG Knowledge Base adapter

- [ ] Version corpus snapshots separately from question datasets.
- [ ] Add mock and live Knowledge Base HTTP adapters.
- [ ] Build reviewed German/English answerable and unanswerable fixtures.
- [ ] Add Recall@k, MRR, and context-precision retrieval scorers.
- [ ] Add citation validity, supported-claim, and refusal scorers.
- [ ] Preserve document IDs, page references, and cited spans as evidence.
- [ ] Separate retrieval failures from generation failures.
- [ ] Add conflicting-document and prompt-injection safety cases.
- [ ] Introduce an optional calibrated LLM judge only after deterministic metrics.

### Definition of done

- Offline RAG evaluation is deterministic and requires no provider credentials.
- Retrieval and generation quality are reported independently.
- Every citation metric links to machine-inspectable evidence.
- Judge agreement is measured against reviewed labels and never treated as truth.

## Phase 5 — Durable control-plane API

- [ ] Add FastAPI request/response contracts without leaking persistence models.
- [ ] Add PostgreSQL repositories and Alembic migrations.
- [ ] Implement projects, targets, datasets, suites, runs, and result APIs.
- [ ] Make run submission idempotent.
- [ ] Enforce append-only results and explicit lifecycle transitions.
- [ ] Add pagination, bounded request sizes, and consistent error responses.
- [ ] Add API integration tests against PostgreSQL.

### Definition of done

- API restarts do not lose accepted run state.
- Duplicate idempotency keys create exactly one run.
- Migrations work from an empty database and the previous schema.
- Partial failures remain visible as structured results.

## Phase 6 — Async execution and reliability

- [ ] Add a Redis-backed durable worker queue.
- [ ] Add bounded concurrency, timeouts, and target rate limits.
- [ ] Classify retryable and terminal failures.
- [ ] Add cancellation, worker leases, heartbeats, and recovery.
- [ ] Make case execution and finalization idempotent.
- [ ] Add failure-injection tests for crashes and duplicate delivery.
- [ ] Run a 1,000-case durability fixture.

### Definition of done

- Worker termination loses and duplicates no final case results.
- Retry exhaustion and partial completion are explicit run outcomes.
- Cancellation is bounded and observable.
- A 1,000-case fixture completes with no missing results.

## Phase 7 — Privacy-safe observability and security

- [ ] Add request/run IDs and structured outcome-only logging.
- [ ] Add OpenTelemetry traces across API, worker, target, and evaluator calls.
- [ ] Export bounded Prometheus metrics for duration, failures, queue depth, and
      evaluation usage.
- [ ] Add sentinel tests proving prompts, responses, SQL, rows, and documents do
      not enter default telemetry.
- [ ] Add authentication and project-scoped authorization.
- [ ] Store only secret references in target configuration.
- [ ] Add dependency, container, and secret scanning.
- [ ] Publish a threat model and recovery procedure.

### Definition of done

- A run can be diagnosed through one linked trace without exposing content.
- Redaction tests find zero sensitive sentinel values in telemetry.
- Authorization matrix tests cover every protected resource.
- No unresolved critical dependency or container vulnerability remains.

## Phase 8 — React evaluation dashboard

- [ ] Add a TypeScript/React application with a generated API client.
- [ ] Build project, dataset, suite, and run views.
- [ ] Build candidate-versus-baseline metric and case comparisons.
- [ ] Add language, task, answerability, and safety slice filters.
- [ ] Add per-case evidence inspection with safe truncation.
- [ ] Add latency and cost distributions with sample counts.
- [ ] Support queued, running, partial, failed, passed, and regressed states.
- [ ] Add Playwright flows and automated accessibility checks.

### Definition of done

- A reviewer can locate a seeded regression and its evidence in under two minutes.
- Every aggregate metric links to its underlying cases.
- Critical workflows are keyboard accessible and covered end to end.
- The UI does not imply certainty when sample sizes are small.

## Phase 9 — Deployment, benchmark, and public release

- [ ] Build non-root production images and a local Docker Compose stack.
- [ ] Select one cloud deployment target in an ADR.
- [ ] Add Terraform, TLS, managed secrets, authentication, and budget controls.
- [ ] Deploy only synthetic or public benchmark data.
- [ ] Publish reproducible DataBridge and RAG benchmark reports.
- [ ] Report task quality, safety, p50/p95 latency, usage/cost, sample counts,
      configuration, variance, and known limitations.
- [ ] Record a 60–90 second regression-gate demo.
- [ ] Publish API/adapter guides, tagged `v0.1.0`, release notes, SBOM, and images.
- [ ] Add bounded `good first issue` candidates and contribution templates.

### Definition of done

- Infrastructure can be created and removed from documented commands.
- A clean deployment passes API, worker, database, UI, and evaluation smoke tests.
- Every published number is reproducible from a tagged dataset and configuration.
- The GitHub landing page demonstrates the regression gate in its first screen.

## Explicit non-goals before the MVP

- Kubernetes and multi-cloud deployment
- Billing and enterprise multi-tenancy
- Visual prompt editing
- Agent orchestration
- A broad model-provider matrix
- Arbitrary user-authored Python scorer execution
- Fine-tuning
- Model-judge scores presented as ground truth
- Synthetic or invented benchmark claims

The MVP boundary is the completed deterministic spine, baseline comparison,
DataBridge adapter, RAG adapter, and CI release gate. Durable distributed
execution and the dashboard deepen the engineering story after that core works.
