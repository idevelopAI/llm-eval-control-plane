# ADR 0012: Define Target-independent Versioned Evaluation Suites

- Status: Proposed
- Date: 2026-09-03

## Context

Run submissions currently select a dataset, target adapter, evaluator set, and
target behavior directly. Comparison submissions separately provide the release
gates. The durable job payload resolves the inputs available at submission, but
there is no single artifact proving that repeated runs used the same evaluation
protocol or that a later comparison applied the policy originally reviewed for
that protocol.

`ArtifactKind.SUITE` was reserved without a suite domain contract. This phase
defines that contract and its canonical identity; a persistence record,
registration API, and evidence link do not yet exist. A suite must be reusable
across candidate and baseline targets, preserve every semantic choice needed to
interpret a run, and remain compatible with the existing immutable artifact and
canonical-digest rules.

Experiment history also needs a clear boundary. A separate mutable experiment
record would duplicate lifecycle already represented by jobs, runs, and release
decisions and could introduce drifting pointers such as a current candidate or
latest decision.

## Decision

### A suite is a target-independent protocol

`EvaluationSuiteVersion` will represent one immutable revision of an evaluation
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
table, or mutable experiment lifecycle. Once suite pinning is implemented, the
experiment history is the append-only relationship among:

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

### Registration and evidence pinning follow this contract

This proposed decision defines the domain boundary only. A later implementation
must add create-once suite registration, PostgreSQL persistence, bounded and
redacted API and CLI contracts, resolved worker payloads, and suite references
covered by new run-result and release-decision digest schemas.

Registration must resolve the dataset and evaluator identities, validate metric
and slice inventories, enforce resource bounds, and treat an identical retry at
one `(name, revision)` as success while conflicting content fails. A worker must
execute the exact suite snapshot pinned at submission rather than resolving a
mutable alias when it claims a job. Comparisons must reject runs with different
suite references or digests as configuration errors and must apply the gates
from the pinned suite rather than accepting a replacement policy.

Existing run and release-decision digest contracts remain valid. Historical
evidence without a suite reference is legacy unpinned evidence and must not be
retroactively assigned an inferred suite.

### Privacy and hosting boundaries do not expand

Canonical suite documents are sensitive control-plane inputs. Future public API
responses must remain explicit redacted projections and must not return dataset
cases, expectations, prompts, target configuration, scenario mappings, outputs,
SQL, rows, credentials, secret references, raw canonical documents, or
operational coordination data. Suite names, digests, evaluator identities,
metric inventories, slices, and gate values remain sensitive metadata available
only through the existing project-bound authorization boundary. Caller-controlled
suite fields must not become metric labels or unreviewed telemetry attributes.

The public Site remains a synthetic, request-free fixture. This proposal adds no
hosted API route, bearer flow, model invocation, persistence, runtime binding,
or application secret. Any synthetic suite presentation added to that artifact
remains subject to ADR 0011 and its build and runtime acceptance gates.

## Consequences

- One digest can identify the complete evaluation protocol independently of the
  target being tested.
- Baseline and candidate evidence can prove that dataset, evaluators, metric
  inventory, slices, execution behavior, and release gates did not drift.
- Editing any semantic setting creates a new immutable suite revision instead
  of changing historical meaning.
- Derived experiment history reuses append-only evidence and avoids another
  mutable lifecycle or synchronization problem.
- Registration, persistence, job payloads, result and decision digests, API and
  CLI contracts, migrations, and dashboard projections require later bounded
  integration before suites are an implemented capability.
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
validated at submission. Durable payloads must eventually pin one resolved
revision and digest before enqueueing.

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
