# ADR 0005: Evaluate DataBridge with Normalized HTTP Evidence and Read-Only PostgreSQL Replay

- Status: Accepted
- Date: 2026-08-20

## Context

DataBridge AI turns English and German questions into PostgreSQL queries. Its
public v1.2.0 API can answer, request clarification, or reject an unsafe or
privacy-sensitive request. An evaluation must measure all three decisions while
preventing untrusted generated SQL from mutating the evaluation fixture.

The provider response also contains fields that are useful to the application
but inappropriate for durable evaluation evidence: natural-language answers,
database rows, request IDs, server timings, and HTTP error bodies. Credentials
and database connection strings are configuration secrets, not artifact content.

SQL parsing alone is not a sufficient execution boundary. SQLGlot documents
that its parser is intentionally lenient, and PostgreSQL functions can have side
effects even inside syntactically valid `SELECT` statements.

## Decision

The adapter implements the pinned DataBridge v1.2.0 contract:

```text
POST /api/v1/query
X-API-Key: <resolved at execution time>
```

Requests contain only `question`, `chat_history`, and `language`. The adapter
normalizes responses into one of three structured states:

- query: generated SQL execution strings and token usage;
- clarification: a language-neutral clarification code;
- refusal: a structured target refusal for HTTP `403`.

Natural-language answers, returned columns and rows, request IDs, provider
timings, headers, and HTTP error bodies are discarded before the target response
crosses the adapter boundary. Technical failures become bounded codes such as
`target_timeout`, `target_rate_limited`, or `target_protocol_error`; raw
exceptions are never persisted.

Mock and live targets have distinct content-addressed identities. A run stores a
provider-neutral execution mode, and comparison rejects evidence from different
modes. Live mode requires an explicit command opt-in and an explicit assertion
that the remote DataBridge deployment uses only a synthetic evaluation database.
The live runner uses a real monotonic clock. Offline mock mode uses a deterministic
clock and makes no network calls.

Every SQL execution is parsed with the PostgreSQL dialect and must satisfy all
of these rules before local replay:

1. the input is within the byte limit and contains no comments;
2. exactly one statement parses;
3. the root is a query;
4. no DDL, DML, command, `INTO`, or locking node exists;
5. referenced tables belong to the reviewed fixture allowlist;
6. system schemas and side-effecting functions are denied.

Approved candidate and reference queries run separately through a PostgreSQL
role that has only database `CONNECT`, schema `USAGE`, and table `SELECT`. The
role cannot create databases, roles, schemas, temporary objects, or bypass row
security. Every execution starts a read-only transaction with statement and lock
timeouts and caps rows, cells, and result bytes. The original SQL is executed;
parser-rendered SQL is never substituted.

Result equivalence compares column names and order independently from rows.
Ordered expectations compare sequences. Unordered expectations compare
multisets so duplicate rows remain significant. Values are converted into a
versioned canonical representation before comparison.

Scorer applicability depends only on reviewed expected behavior. A candidate
that returns the wrong response kind therefore scores zero instead of changing
its own coverage. Invalid candidate SQL is a scored failure. Invalid reference
SQL, a corrupt database fixture, or evaluator infrastructure failure is an error
observation and fails release coverage.

## Consequences

- HTTP `403` is measurable refusal behavior rather than a technical outage.
- Mock accuracy and simulated latency cannot be presented as live evidence.
- The API key, DSN, rows, answers, request IDs, and provider errors are excluded
  from target identity and default reports.
- Parser policy, database permissions, read-only transactions, resource limits,
  and an unchanged database fingerprint provide independent safety layers.
- Live evaluation cannot make an unknown production database safe; it is allowed
  only for an explicitly confirmed synthetic DataBridge deployment.
- SQLGlot, the psycopg driver, replay limits, normalization, policy, and fixture
  identities are part of evaluator identity. Continuous integration also pins
  the PostgreSQL image by digest, so changes produce new evidence rather than
  silently reinterpreting old runs.
