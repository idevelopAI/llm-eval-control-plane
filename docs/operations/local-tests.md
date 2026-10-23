# Local database-backed tests

The offline checks in [Contributing](../../CONTRIBUTING.md#quality-checks) do
not need a server. The full Python coverage run also exercises PostgreSQL
leases, recovery, and evidence publication. It needs a disposable database and
the current Alembic schema, just like the `Test Python` jobs in
[CI](../../.github/workflows/ci.yml).

## Disposable control-plane database

Requirements: Python 3.11+, uv, Bash, and a running Docker engine with Docker
Compose available. Run the complete block from the repository root. Image and
dependency downloads may need network access; no provider key or paid API call
is used. Keep the image below aligned with the CI workflow's pinned image.

**Never use an existing development or production database.** Integration tests
write and delete records. This block creates its own container, stores PostgreSQL
data in temporary memory, and removes that container on normal exit, test
failure, or a handled interrupt. It does not mount repository files, touch
Compose volumes, or start the application stack. A hard process kill or machine
crash may prevent the cleanup trap from running; only stop a leftover container
after identifying it by the `llm-eval-purpose=local-tests` label.

Like CI, this disposable instance uses PostgreSQL `trust` authentication, with
no password. Its randomly assigned host port is bound only to `127.0.0.1`, but
other local processes can still connect. Use it only for synthetic test data
on a trusted workstation, never on a shared/untrusted host or for real evidence.
The subshell clears inherited database settings so neither these tests nor the
DataBridge tests accidentally connect to another configured database. Your
parent shell's environment is unchanged.

```bash
bash <<'BASH'
set -euo pipefail
unset CONTROL_PLANE_DATABASE_URL CONTROL_PLANE_DATABASE_HOST
unset CONTROL_PLANE_DATABASE_PORT CONTROL_PLANE_DATABASE_NAME
unset CONTROL_PLANE_DATABASE_USER CONTROL_PLANE_DATABASE_PASSWORD_FILE
unset DATABRIDGE_EVAL_DSN

uv sync --locked
test_container=$(docker run --detach --rm \
  --label llm-eval-purpose=local-tests \
  --tmpfs /var/lib/postgresql:rw \
  --publish 127.0.0.1::5432 \
  --env POSTGRES_DB=control_plane \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  --health-cmd 'pg_isready -h 127.0.0.1 -U postgres -d control_plane' \
  --health-interval 1s --health-timeout 3s --health-retries 30 \
  postgres:18.6-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2)
trap 'docker stop --time 5 "$test_container" >/dev/null' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

test_wait=0
until [ "$(docker inspect --format '{{.State.Health.Status}}' "$test_container")" = healthy ]; do
  test_wait=$((test_wait + 1))
  if [ "$test_wait" -ge 60 ]; then
    printf '%s\n' 'Disposable PostgreSQL did not become healthy.' >&2
    exit 1
  fi
  sleep 1
done

test_binding=$(docker port "$test_container" 5432/tcp)
case "$test_binding" in
  127.0.0.1:*) ;;
  *) printf '%s\n' 'Unexpected test database binding.' >&2; exit 1 ;;
esac
test_port=${test_binding##*:}
export CONTROL_PLANE_DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:${test_port}/control_plane"

uv run --locked alembic upgrade head
uv run --locked pytest \
  --cov=llm_eval_control_plane --cov-branch --cov-report=term-missing
BASH
```

The expected result is a passing run with the configured 95% coverage minimum.
The control-plane PostgreSQL tests must execute, not report missing
`CONTROL_PLANE_DATABASE_URL`. The URL contains no credential and exists only
inside this subshell. Coverage and pytest caches remain locally gitignored.

## Separate DataBridge fixture

The command above intentionally unsets `DATABRIDGE_EVAL_DSN`. The tests in
`tests/integration/test_databridge_postgres_workflow.py` therefore skip, just as
they do in the main CI Python matrix. They are covered separately by the
[DataBridge Offline Gate](../../.github/workflows/databridge-gate.yml).

To run those tests locally, follow the
[DataBridge PostgreSQL guide](databridge-evaluation.md) to seed a separate,
disposable fixture and provision its restricted read-only role. With that
role's `DATABRIDGE_EVAL_DSN` configured securely, run:

```bash
uv run --locked pytest -q tests/integration/test_databridge_postgres_workflow.py
```

Do not point DataBridge at the control-plane database or use its administrative
role for replay. No live DataBridge endpoint or API key is needed for these
mock-response integration tests. The Python coverage run is not a substitute
for the dashboard, image/security, and dedicated workflow gates.
