import json
from pathlib import Path

from pytest import mark

from llm_eval_control_plane.adapters.sql_policy import (
    PostgresSqlPolicy,
    SqlPolicyReason,
)


@mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM employees ORDER BY id",
        "SELECT d.name, COUNT(e.id) FROM public.departments d "
        "LEFT JOIN employees e ON e.department_id = d.id GROUP BY d.name",
        "SELECT ROUND(AVG(budget), 2), SUM(budget) FROM projects",
        "WITH ranked AS (SELECT ROW_NUMBER() OVER (ORDER BY salary) AS n "
        "FROM employees) SELECT n FROM ranked",
        "SELECT DATE '2026-01-01' AS start_date",
        "SELECT '--not a comment' AS marker FROM employees LIMIT 1",
        'SELECT "/*not_comment*/" FROM employees',
    ],
)
def test_policy_allows_reviewed_postgres_queries(sql: str) -> None:
    result = PostgresSqlPolicy().evaluate(sql)

    assert result.parse_valid is True
    assert result.allowed is True
    assert result.reason_code == SqlPolicyReason.ALLOWED


@mark.parametrize(
    "sql",
    [
        "SELECT id FROM employees -- hidden second intent",
        "SELECT id /* hidden */ FROM employees",
    ],
)
def test_policy_rejects_actual_comments(sql: str) -> None:
    result = PostgresSqlPolicy().evaluate(sql)

    assert result.parse_valid is True
    assert result.allowed is False
    assert result.reason_code == SqlPolicyReason.COMMENTS_NOT_ALLOWED


def test_policy_distinguishes_invalid_and_multiple_statements() -> None:
    invalid = PostgresSqlPolicy().evaluate("SELECT FROM")
    multiple = PostgresSqlPolicy().evaluate(
        "SELECT id FROM employees; SELECT id FROM projects"
    )

    assert invalid.parse_valid is False
    assert invalid.reason_code == SqlPolicyReason.INVALID_SQL
    assert multiple.parse_valid is True
    assert multiple.allowed is False
    assert multiple.reason_code == SqlPolicyReason.MULTIPLE_STATEMENTS


@mark.parametrize(
    "sql",
    [
        "DELETE FROM employees",
        "UPDATE employees SET name = 'x'",
        "INSERT INTO employees (id) VALUES (99)",
        "DROP TABLE employees",
        "ALTER TABLE employees ADD COLUMN secret text",
        "COPY employees TO '/tmp/data'",
    ],
)
def test_policy_requires_a_query_statement(sql: str) -> None:
    result = PostgresSqlPolicy().evaluate(sql)

    assert result.parse_valid is True
    assert result.allowed is False
    assert result.reason_code in {
        SqlPolicyReason.NON_QUERY_STATEMENT,
        SqlPolicyReason.PROHIBITED_OPERATION,
    }


@mark.parametrize(
    "sql",
    [
        "SELECT * INTO archived_employees FROM employees",
        "SELECT id FROM employees FOR UPDATE",
        "SELECT id FROM employees FOR SHARE",
    ],
)
def test_policy_rejects_query_shaped_write_or_lock_operations(sql: str) -> None:
    result = PostgresSqlPolicy().evaluate(sql)

    assert result.parse_valid is True
    assert result.allowed is False
    assert result.reason_code == SqlPolicyReason.PROHIBITED_OPERATION


@mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT * FROM users", SqlPolicyReason.TABLE_NOT_ALLOWED),
        ("SELECT * FROM private.employees", SqlPolicyReason.TABLE_NOT_ALLOWED),
        ("SELECT * FROM pg_catalog.pg_user", SqlPolicyReason.SYSTEM_SCHEMA),
        (
            "SELECT * FROM information_schema.tables",
            SqlPolicyReason.SYSTEM_SCHEMA,
        ),
    ],
)
def test_policy_enforces_table_and_schema_allowlists(
    sql: str, reason: SqlPolicyReason
) -> None:
    result = PostgresSqlPolicy().evaluate(sql)

    assert result.allowed is False
    assert result.reason_code == reason


@mark.parametrize(
    "function_call",
    [
        "pg_sleep(1)",
        "set_config('x', 'y', false)",
        "setval('sequence', 1)",
        "nextval('sequence')",
        "pg_advisory_lock(1)",
        "dblink('x', 'SELECT 1')",
        "lo_export(1, '/tmp/x')",
        "lo_import('/tmp/x')",
        "pg_read_file('/etc/passwd')",
        "pg_terminate_backend(1)",
        "mystery_side_effect()",
    ],
)
def test_policy_rejects_prohibited_and_unknown_functions(
    function_call: str,
) -> None:
    result = PostgresSqlPolicy().evaluate(f"SELECT {function_call}")

    assert result.parse_valid is True
    assert result.allowed is False
    assert result.reason_code == SqlPolicyReason.PROHIBITED_FUNCTION


def test_policy_rejects_utf8_source_over_byte_cap() -> None:
    policy = PostgresSqlPolicy(max_sql_bytes=20)

    result = policy.evaluate("SELECT 'éééééé'")

    assert result.parse_valid is False
    assert result.allowed is False
    assert result.reason_code == SqlPolicyReason.SQL_TOO_LARGE


def test_policy_result_and_configuration_never_include_source_sql() -> None:
    source = "SELECT private_value FROM forbidden_table"
    policy = PostgresSqlPolicy()
    result = policy.evaluate(source)

    assert source not in repr(result)
    assert source not in str(policy.semantic_config)
    assert policy.semantic_config["dialect"] == "postgres"
    allowed_functions = policy.semantic_config["allowed_functions"]
    assert isinstance(allowed_functions, list)
    assert "count" in allowed_functions


def test_policy_configuration_is_validated() -> None:
    try:
        PostgresSqlPolicy(allowed_tables=frozenset())
    except ValueError as error:
        assert str(error) == "SQL policy requires at least one allowed table"
    else:
        raise AssertionError("empty allowlist was accepted")


def test_pinned_adversarial_sql_is_rejected() -> None:
    project_root = Path(__file__).parents[3]
    payload = json.loads(
        (project_root / "examples/databridge/adversarial-sql-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(payload, dict)
    cases = payload["cases"]
    assert isinstance(cases, list) and len(cases) == 12
    policy = PostgresSqlPolicy()
    for case in cases:
        assert isinstance(case, dict)
        sql = case["sql"]
        assert isinstance(sql, str)
        assert policy.evaluate(sql).allowed is False
