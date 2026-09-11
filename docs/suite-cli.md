# Offline suite workflow

`llm-eval suite` resolves a protocol once, runs baseline and candidate targets
against that exact protocol, and applies its pinned release policy. It needs no
server, Docker, GitHub credential, provider key, or paid API call. The target,
latency, and usage are deterministic synthetic fixtures, not live model
measurements. These commands do not connect to the HTTP API or PostgreSQL.

## Build and validate

After installing the locked environment with `uv sync --locked`, run:

```bash
mkdir -p .llm-eval
uv run llm-eval suite schema
uv run llm-eval suite build \
  examples/release-suite.json examples/release-gate-40.jsonl \
  --output .llm-eval/release-suite-v1.json
uv run llm-eval suite validate \
  .llm-eval/release-suite-v1.json examples/release-gate-40.jsonl
```

The authoring document selects a suite name/revision, dataset name/revision,
built-in evaluator names, declared slices, and gates. Build resolves the actual
dataset digest, installed evaluator identities and metric inventories, and the
fixed `deterministic_fake` / `offline_mock` serial execution contract. It computes
the suite digest and writes a canonical `EvaluationSuiteVersion`. Input order
and formatting do not affect the digest.

Authoring documents cannot contain targets, credentials, live endpoints,
execution overrides, supplied digests, or unknown fields. Revisions and execution
counts are strict integers; thresholds are finite numbers, not strings or booleans.
Limits are seven distinct built-in evaluators, 32 resolved metrics, 128 declared
slices, and 64 gates. Definitions, suite files, and scenario maps are bounded to
256 KiB. Duplicate keys, non-finite numbers, invalid UTF-8, and excessively nested
JSON fail validation. Suite inputs must be regular files, not symlinks or pipes.

Build and validate do not construct or invoke a target. Validate checks the
digest, exact dataset content, declared slice membership, and current installed
evaluator contract. Standard output contains only artifact identities, execution
mode, and counts, not complete gates or dataset cases.

Build is create-only: existing output paths are never overwritten, even for an
identical retry. Choose a new path when rebuilding. Complete temporary bytes are
atomically published as an owner-only file on POSIX. The CLI does not maintain a
name/revision registry; PostgreSQL registration provides cross-file uniqueness.
Policy changes should use a new revision. Evidence always pins the complete
suite name, revision, and digest.

## Run both targets

```bash
uv run llm-eval suite run \
  .llm-eval/release-suite-v1.json examples/release-gate-40.jsonl \
  --run-id suite-baseline --target-name fake/release --target-revision 1

uv run llm-eval suite run \
  .llm-eval/release-suite-v1.json examples/release-gate-40.jsonl \
  --run-id suite-candidate --target-name fake/release --target-revision 2 \
  --scenario-overrides examples/release-regression-overrides.json
```

Only target identity and deterministic case scenarios can differ. Dataset,
evaluator, execution, and policy overrides are not accepted. Scenario maps have
at most 1,000 string-valued entries; unknown case IDs fail before target
construction. Runs use the existing append-only local store in `.llm-eval/runs/`;
`--store` selects another root. Identical run-ID retries produce identical
evidence; conflicting retries preserve the stored artifact and return an error.

The standard run summary and `llm-eval show RUN_ID` include the resolved `suite`
reference on pinned evidence. Unpinned summaries retain their old shape. Full
local run artifacts still contain evaluation data; summaries omit raw inputs,
expected answers, outputs, and scenario mappings.

## Compare the pinned evidence

```bash
uv run llm-eval suite compare \
  .llm-eval/release-suite-v1.json examples/release-gate-40.jsonl \
  --baseline-run suite-baseline --candidate-run suite-candidate \
  --format markdown --output .llm-eval/suite-release-report.md
```

This regression fixture intentionally produces a failed release decision and exit
code `1`: the comparison identified regressions, rather than failing to execute.
Omit scenario overrides on a fresh candidate run to exercise passing evidence.

Comparison derives policy only from the suite. It rejects mixed pinned/unpinned
runs, different suite identities or digests, dataset drift, and mismatched
evaluator evidence. It does not re-resolve or invoke installed evaluators, so
historical comparisons do not depend on today's scorer implementation.

JSON, Markdown, and JUnit reports include suite identity and digest. Reports omit
raw inputs, expected answers, and outputs, but contain potentially sensitive
protocol metadata and case IDs. Keep generated files in gitignored `.llm-eval/`
or another protected location. Report output files are create-only. Without
`--output`, the report is printed to standard output.

| Exit code | Meaning |
| --- | --- |
| `0` | Build/validation succeeded; evaluation completed; or release gates passed |
| `1` | Evaluation recorded execution failures, or comparison found failed gates |
| `2` | Invalid input, dependency/integrity mismatch, unsupported options, or storage failure |

The existing unpinned `llm-eval run` / `compare` workflow is unchanged. These
commands do not register suites remotely or change the public Site.
