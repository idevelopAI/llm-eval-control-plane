# ADR 0012: Define Target-independent Versioned Evaluation Suites

- Status: Accepted
- Date: 2026-09-03
- Accepted: 2026-09-07

## Context

Legacy run submissions select a dataset, target adapter, evaluator set, and
target behavior directly. Comparison submissions separately provide the release
gates. Their durable job payload resolves the inputs available at submission,
but does not identify a single artifact proving that repeated runs used the same
evaluation protocol or that a later comparison applied the policy originally
reviewed for that protocol.

`ArtifactKind.SUITE` was originally reserved without a suite domain contract.
The frozen contract and its canonical identity now exist, together with an
application registration service, PostgreSQL record, snapshot-pinned worker
execution, digest-bound links from run and release evidence, authenticated suite
HTTP surfaces, and local offline suite CLI commands. A suite must be reusable
across candidate and baseline targets, preserve every semantic choice needed to
interpret a run, and remain compatible with the existing immutable artifact and
canonical-digest rules.

Experiment history also needs a clear boundary. A separate mutable experiment
record would duplicate lifecycle already represented by jobs, runs, and release
decisions and could introduce drifting pointers such as a current candidate or
latest decision.

## Decision

### A suite is a target-independent protocol

`EvaluationSuiteVersion` represents one immutable revision of an evaluation
protocol. It contains:

- a resolved dataset `ArtifactRef` with its digest;
- one or more canonically ordered `SuiteEvaluator` bindings;
- a canonically ordered set of declared dataset slices;
- one `SuiteExecutionSettings` value; and
- one or more canonically ordered release gates.

The suite name and positive revision provide its author-facing artifact
identity. Its resolved `ArtifactRef` has kind `suite` and includes the suite
content digest.

A `SuiteEvaluator` binds a bounded executor name to one resolved evaluator
`ArtifactRef` and its complete, nonempty metric inventory. Executor names,
evaluator logical keys, and metric names are unique. Metric inventories and the
binding collection use canonical order. A release gate may name only a metric
declared by exactly one binding.

Declared slices are exact labels already present in the resolved dataset. They
are unique and lexicographically ordered. A sliced release gate may reference
only a declared slice. The dataset continues to determine case membership; a
suite slice declaration neither rewrites the dataset nor exposes a slice label
to the target.

`SuiteExecutionSettings` contains only semantic execution behavior shared by
all targets evaluated under the suite:

- the bounded adapter identifier;
- the exact `ExecutionMode`;
- canonical case-ID order;
- one target invocation per case; and
- serial execution with concurrency fixed to one.

The fixed ordering, invocation count, and concurrency values make the initial
contract explicit without claiming support for parallel or repeated sampling.
Target identity is deliberately absent. A baseline and candidate can therefore
use the same suite, while each run continues to record its own resolved target
artifact. A target used with a suite must be compatible with the suite adapter
and execution mode.

### Suite content has one canonical digest

The suite digest uses RFC 8785 canonical JSON and SHA-256 with the digest-schema
label `evaluation-suite/v1`. The semantic digest envelope covers:

- the resolved dataset reference;
- every canonical evaluator binding and metric inventory;
- every declared slice;
- all suite execution settings; and
- every release gate, including direction, threshold, absolute regression
  budget, and optional slice.

Evaluator bindings are ordered by evaluator logical key, metric inventories and
slices are ordered lexicographically, and gates are ordered by metric and
optional slice. Input order is not semantic. Defaults for included semantic
fields are materialized before hashing.

The digest excludes the suite name, revision, registration timestamp, source
location, formatting, and other authoring metadata. It also excludes
credentials, provider secrets, database configuration, worker leases,
heartbeats, retry limits, retry delays, queue placement, and other operational
coordination settings. Credentials and raw secret values are not valid suite
fields at all. A digest proves content integrity; it does not encrypt the suite
or grant access to it.

Changing any covered field creates different suite content and requires a new
published revision. Changing the canonical envelope or its normalization rules
requires a new digest-schema label and an explicit compatibility path.

### Experiment history is derived from immutable evidence

The control plane will not add a separate `ExperimentDefinition`, experiment
table, or mutable experiment lifecycle. Experiment history is defined as the
append-only relationship among:

- a resolved suite revision;
- runs that pin that exact suite digest and their resolved target artifacts;
  and
- release decisions that pin the same suite and the exact baseline and
  candidate run evidence.

Repeated decisions for the same resolved suite, baseline target, and candidate
target form a derived history. Unpaired runs remain discoverable under their
suite and target. Completion, failure, newest evidence, and current release
outcome are projections over immutable jobs, runs, and decisions rather than
fields updated on an experiment record.

This design preserves the existing sources of truth: jobs describe execution
lifecycle, runs describe evaluated evidence, and release decisions connect two
exact runs through a policy. It avoids a second state machine and prevents a
mutable experiment pointer from changing the meaning of historical evidence.
Suite-pinned evidence and exact-suite history queries are implemented. History
uses nullable relational projections populated atomically with pinned evidence,
with all-or-none constraints and suite/time/ID indexes. The maintenance migration
backfills only explicit existing pins without rewriting canonical documents.
Collection reads select only metadata, newest first, with cursors bound to the
complete suite pin and stream. The local dashboard presents catalog, run, and
decision metadata with explicit pagination and at most 100 records retained per
collection. It validates exact suite pins and cancels superseded reads without
changing the hosted fixture. History rows can open detailed gate review after
matching the complete suite pin and immutable decision metadata. Only the active
historical selection supplements the bounded recent-decision picker; cases and
distributions retain their existing redacted projections. Metadata-only target
and directed target-pair discovery now derives groups from persisted suite runs
and decision-to-run joins. Exact target filters bind name, revision, and digest
into history cursors without introducing mutable experiment records or new
schema projections. Group discovery uses bytewise identity ordering, while
filtered evidence remains newest-first. The local dashboard implements separate
run-target and directed target-pair selectors, bounded group pagination, history
cursor resets, and exact pair verification before historical gate review. These
controls remain outside the hosted fixture build.

### Registration is create-once; jobs pin complete snapshots

The application service and PostgreSQL repository implement create-once suite
registration. Before a new record is written, registration loads the exact
dataset revision and requires its resolved reference, digest, and declared
slices to match. It asks the selected executor to resolve the adapter and
evaluator names, then requires exact execution settings, evaluator references,
and metric inventories. Validation failures use content-safe application errors
and do not persist a partial suite.

The `control_plane_suites` table stores the canonical suite document with its
digest, dataset identity, bounded evaluator, metric, slice, and gate counts,
execution mode, and registration time. Its composite foreign key restricts
removal of the resolved dataset. Detail reads recalculate the suite model and
cross-check every indexed projection. Collection reads select only bounded
indexed metadata, use stable keyset pagination, and can filter by exact name.

An identical retry at one `(name, revision)` returns the durable record without
re-resolving current dataset or executor dependencies. Different content at that
identity conflicts. Different revisions may intentionally share one semantic
digest because suite name and revision do not enter content identity.

The application service provides `submit_suite_run` and
`submit_suite_comparison`. Each resolves an exact registered suite name and
revision for a new submission. The run path verifies the dataset and executor
contract and pins the complete suite plus the resolved target contract in a
`run-job/v2` payload. The comparison path derives the policy from the suite and
pins the complete suite, compiled specification, and exact baseline and
candidate result digests in a `comparison-job/v2` payload. Both paths use the
existing atomic enqueue and semantic idempotency boundary. An exact replay
returns the original job before resolving dependencies again.

A worker executes the pinned snapshot without reloading the suite registry or
resolving a mutable alias. Run execution rejects drift in the available executor
settings, evaluator identities, or metric inventories before target invocation.
Comparisons require both runs to match the complete suite reference and the
policy compiled from its snapshot. Different suite names, revisions, or digests,
mixed pinned/unpinned evidence, and replacement policies are configuration
errors, not failed release gates.

### Evidence pinning preserves legacy digest contracts

`RunResult.suite` and `ReleaseDecision.suite` are optional resolved artifact
references. When present, `run-result/v3` and `release-decision/v3` digest
envelopes cover the complete suite reference, including name, revision, and
digest, plus the explicit execution mode. The reference binds evidence to the
reviewed protocol even when otherwise-identical suite content is registered
under another revision.

Without a suite reference, deterministic fixture evidence retains its v1 digest
envelope and other execution modes retain v2. Serializers omit the absent suite
field, preserving historical canonical document bytes as well as digests.
Legacy v1 job payloads likewise omit a suite snapshot; v2 payloads require one.
Historical evidence remains unpinned and is not assigned an inferred suite.

The offline suite CLI builds and validates canonical protocol files, runs pinned
evaluations, and compares immutable evidence without network calls or a database.
It does not register suites remotely.

Suite HTTP registration, bounded metadata collection, revision detail, and
asynchronous run/comparison routes are implemented under the existing project
authorization boundary. Run and decision detail projections expose the resolved
suite reference only when present; legacy unpinned responses omit it. The new
submission routes require an exact suite name/revision and prohibit replacement
dataset, evaluator, execution, or gate settings. Existing legacy submission
endpoints and CLI commands keep their unpinned contract. Suite-history dashboard
views remain outside the implemented surface.

### Privacy and hosting boundaries do not expand

Canonical suite documents are sensitive control-plane inputs. Suite API
responses use explicit metadata projections and do not return dataset
cases, expectations, prompts, target configuration, scenario mappings, outputs,
SQL, rows, credentials, secret references, raw canonical documents, or
operational coordination data. Suite names, digests, evaluator identities,
metric inventories, slices, and gate values remain sensitive metadata available
only through the existing project-bound authorization boundary. Caller-controlled
suite fields must not become metric labels or unreviewed telemetry attributes.

The public Site remains a synthetic, request-free fixture. Suite registration
and execution add no hosted API route, bearer flow, model invocation, runtime
binding, or application secret. Any synthetic suite presentation added to that
artifact remains subject to ADR 0011 and its build and runtime acceptance gates.

## Consequences

- One digest can identify the complete evaluation protocol independently of the
  target being tested.
- Baseline and candidate evidence can prove that dataset, evaluators, metric
  inventory, slices, execution behavior, and release gates did not drift.
- Editing any semantic setting creates a new immutable suite revision instead
  of changing historical meaning.
- Derived experiment history reuses append-only evidence and avoids another
  mutable lifecycle or synchronization problem.
- Create-once application registration, PostgreSQL persistence, migration, and
  bounded repository projections are implemented without exposing a new public
  route.
- Suite-backed payloads, application submissions, worker execution, and new
  evidence digest envelopes are implemented without changing legacy evidence
  bytes or exposing canonical suite documents through an HTTP route.
- Suite HTTP contracts and optional detail-response provenance are implemented.
  The offline CLI provides a local file-based workflow. Exact-suite run and
  decision history queries and the bounded, local-only suite-history dashboard
  are implemented; hosted live history remains disabled.
- Initial execution remains deliberately serial and single-invocation. A future
  sampling or concurrency model requires a new reviewed semantic contract.

## Non-goals

- Target definitions, credentials, provider endpoints, or database secrets in a
  suite.
- Mutable aliases such as `latest`, in-place editing, drafts, promotion, or
  deletion of published suite revisions.
- A separate experiment resource, experiment table, mutable experiment status,
  current-candidate pointer, or champion registry.
- Automatic experiment orchestration, grid search, hyperparameter optimization,
  repeated sampling, statistical-significance claims, or parallel execution.
- Arbitrary slice expressions or dynamic cohort queries; declared slices are
  exact labels in the immutable dataset.
- Arbitrary evaluator plugins, model-based judges, provider-backed API
  execution, billing, hosted live access, or multi-project row tenancy.
- Retroactively inferring a suite for historical runs or decisions.

## Rejected alternatives

### Include the target in the suite

A target-bound suite would require separate otherwise-identical suite revisions
for baseline and candidate runs and would prevent the suite digest from proving
that both arms used one shared protocol. Targets remain independently resolved
run artifacts.

### Repeat suite settings on every run and comparison

Independent request fields permit accidental drift and cannot identify one
reviewed protocol. A resolved suite reference makes that relationship explicit
and content-addressed.

### Resolve a `latest` suite when a worker claims work

Queue delay, retry, or recovery could select different content from the content
validated at submission. Durable payloads instead pin the complete resolved
suite snapshot before enqueueing.

### Store a mutable experiment registry

A mutable registry would duplicate job state and need synchronization with
append-only runs and decisions. Derived grouping preserves history without
allowing a pointer update to reinterpret old evidence.

### Include operational retry and lease settings in suite identity

Those values coordinate delivery and recovery; they do not define the intended
evaluation protocol. Including them would create different suite identities for
operationally equivalent evidence and blur the boundary established by ADRs
0006 and 0007.

### Expose canonical suite documents to the dashboard

The document can contain sensitive evaluation metadata and is unnecessary for
release review. The browser boundary must continue to receive only bounded,
purpose-built projections.
