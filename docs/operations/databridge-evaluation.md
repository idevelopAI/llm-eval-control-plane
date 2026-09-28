# DataBridge PostgreSQL evaluation

Run these commands from the repository root. This optional workflow requires a
local PostgreSQL fixture; it is separate from the database-free [offline suite
quickstart](../suite-cli.md). See the [adapter design](../databridge-adapter.md)
for the request contract, normalization, and SQL replay boundary.

The pinned DataBridge fixture contains 56 reviewed cases: 40 source query cases,
eight ambiguity cases, and eight unsafe or privacy-sensitive requests. English
and German are balanced at 28 cases each. The source cases and PostgreSQL seed
are pinned to DataBridge AI `v1.2.0` commit
`27b4a6ea96a8aec331afe758cc78dff50a1c6690`; artifact hashes are recorded in
[`examples/databridge/provenance-v1.json`](../../examples/databridge/provenance-v1.json).

Create an empty, disposable PostgreSQL database, seed it, and provision a
separate evaluation role with only `CONNECT`, schema `USAGE`, and table `SELECT`
permissions. The seed intentionally creates neither a role nor a credential.

Configure a `databridge-seed` entry in a PostgreSQL [connection service
file](https://www.postgresql.org/docs/current/libpq-pgservice.html) outside Git
with the local fixture's host, port, database, and administrative user. Store its
password separately in a [password
file](https://www.postgresql.org/docs/current/libpq-pgpass.html), also outside Git
and mode `0600` on POSIX. Keep both files protected; use `PGSERVICEFILE` and
`PGPASSFILE` for their paths, not credential values. Do not embed a password in
the service entry, command arguments, or shell history.

The service name below is non-secret and applies only to this seeding command;
do not export an administrative `PGSERVICE` into the evaluation process. The
evaluation DSN must use the separate restricted role.

```bash
test -r "${PGSERVICEFILE:?}"
test -r "${PGPASSFILE:?}"
psql 'service=databridge-seed' -v ON_ERROR_STOP=1 \
  -f examples/databridge/postgres-fixture-v1.sql

# Set this out of band to the restricted evaluation role; do not paste it into
# a command, tracked file, or shell history.
test -n "${DATABRIDGE_EVAL_DSN:-}"

uv sync --locked
uv run llm-eval databridge run examples/databridge/cases-v1.jsonl \
  --run-id databridge-mock-v1 \
  --fixture-sql examples/databridge/postgres-fixture-v1.sql \
  --expected-fixture-fingerprint \
    sha256:e40acff961cc83377391195acb15d09fa2931b1cc9b3dd01ee03fcc043a21a09 \
  --responses examples/databridge/mock-responses-v1.json \
  --target-revision 1
```

Mock mode performs no target HTTP calls. It replays strict, checked-in
DataBridge wire responses through the same normalizer as the HTTP adapter, then
executes allowed SQL against the local PostgreSQL fixture. The composite scorer
records interaction decision and clarification correctness, unsafe-query
rejection, PostgreSQL parse and read-only-policy results, execution success,
column equivalence, and ordered or unordered result-set equivalence. Latency and
usage metrics are also retained. The connected database must match the pinned
normalized fingerprint before a run, and the same fingerprint must remain after
the run.

The four-case override demonstrates query-result, clarification, and unsafe-SQL
regressions without changing the reviewed dataset:

```bash
uv run llm-eval databridge run examples/databridge/cases-v1.jsonl \
  --run-id databridge-mock-regression-v2 \
  --fixture-sql examples/databridge/postgres-fixture-v1.sql \
  --expected-fixture-fingerprint \
    sha256:e40acff961cc83377391195acb15d09fa2931b1cc9b3dd01ee03fcc043a21a09 \
  --responses examples/databridge/mock-responses-v1.json \
  --response-overrides examples/databridge/regression-overrides-v2.json \
  --target-revision 2

# Expected exit code: 1, because six release gates detect the four regressions.
uv run llm-eval compare \
  examples/databridge/release-policy-v1.json \
  examples/databridge/cases-v1.jsonl \
  --baseline-run databridge-mock-v1 \
  --candidate-run databridge-mock-regression-v2
```

The offline proof completes all 56 baseline cases without technical failures
and passes all seven release gates. The four seeded regressions are then blocked
by six gates covering overall and German decision accuracy, clarification,
unsafe-query rejection, read-only policy, and result equivalence. The dedicated
`DataBridge Offline Gate` check reproduces both outcomes with a digest-pinned
PostgreSQL 18.6 image and no DataBridge API credential.

> **Evidence boundary:** mock target responses, target latency, and token usage
> are deterministic simulations. PostgreSQL replay is real local execution, but
> the mock workflow is not evidence of a deployed model's accuracy or
> performance. Live accuracy was not run or reported for this release.

Live mode calls the DataBridge `/api/v1/query` endpoint only after two explicit
opt-ins. Both the API key and the restricted replay DSN are read from named
environment variables; their values are not accepted as CLI options.

```bash
# Set DATABRIDGE_API_KEY and DATABRIDGE_EVAL_DSN through your secret manager.
test -n "${DATABRIDGE_API_KEY:-}"
test -n "${DATABRIDGE_EVAL_DSN:-}"

uv run llm-eval databridge run examples/databridge/cases-v1.jsonl \
  --run-id databridge-live-v1 \
  --fixture-sql examples/databridge/postgres-fixture-v1.sql \
  --expected-fixture-fingerprint \
    sha256:e40acff961cc83377391195acb15d09fa2931b1cc9b3dd01ee03fcc043a21a09 \
  --live-base-url https://databridge.example \
  --allow-live \
  --confirm-synthetic-database \
  --target-name databridge/live \
  --target-revision 1
```

`--live-base-url` must be an HTTPS origin without credentials, a path, query, or
fragment. Plain HTTP is rejected unless `--allow-insecure-loopback` is supplied
for an explicit loopback development endpoint. Mock response options cannot be
combined with live mode.
