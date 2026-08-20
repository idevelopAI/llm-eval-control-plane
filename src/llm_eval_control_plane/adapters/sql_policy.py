"""Pure PostgreSQL read-only policy used before every SQL replay."""

from __future__ import annotations

import re
from enum import StrEnum

import sqlglot
from sqlglot import exp

from llm_eval_control_plane.domain.execution import SafeCode
from llm_eval_control_plane.domain.models import FrozenModel

POSTGRES_DIALECT = "postgres"
DEFAULT_MAX_SQL_BYTES = 32_768
DEFAULT_ALLOWED_TABLES = frozenset({"departments", "employees", "projects"})
_ALLOWED_SCHEMAS = frozenset({"", "public"})
_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})
_PROHIBITED_NODE_NAMES = frozenset(
    {
        "Alter",
        "Analyze",
        "Attach",
        "Cache",
        "Command",
        "Copy",
        "Create",
        "Delete",
        "Detach",
        "Drop",
        "Execute",
        "Grant",
        "Insert",
        "Into",
        "LoadData",
        "Lock",
        "Merge",
        "Pragma",
        "Replace",
        "Revoke",
        "Set",
        "Transaction",
        "TruncateTable",
        "Update",
        "Use",
    }
)
_PROHIBITED_FUNCTIONS = frozenset(
    {
        "clock_timestamp",
        "dblink",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_disconnect",
        "dblink_exec",
        "dblink_open",
        "gen_random_uuid",
        "lo_export",
        "lo_import",
        "nextval",
        "pg_backend_pid",
        "pg_cancel_backend",
        "pg_logdir_ls",
        "pg_ls_archive_statusdir",
        "pg_ls_dir",
        "pg_ls_logdir",
        "pg_ls_tmpdir",
        "pg_ls_waldir",
        "pg_notify",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_sleep",
        "pg_stat_file",
        "pg_terminate_backend",
        "random",
        "set_config",
        "setval",
        "timeofday",
        "uuid_generate_v1",
        "uuid_generate_v4",
    }
)
_PROHIBITED_FUNCTION_PREFIXES = (
    "dblink_",
    "lo_",
    "pg_advisory_",
    "pg_read_",
    "pg_write_",
)
_ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "and",
        "avg",
        "cast",
        "ceil",
        "ceiling",
        "coalesce",
        "count",
        "date_trunc",
        "dense_rank",
        "extract",
        "floor",
        "greatest",
        "lag",
        "lead",
        "least",
        "length",
        "lower",
        "max",
        "min",
        "nullif",
        "not",
        "or",
        "rank",
        "replace",
        "round",
        "row_number",
        "substring",
        "sum",
        "trim",
        "upper",
    }
)
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


class SqlPolicyReason(StrEnum):
    """Stable, content-free reason returned by the SQL policy."""

    ALLOWED = "allowed"
    COMMENTS_NOT_ALLOWED = "comments_not_allowed"
    INVALID_SQL = "invalid_sql"
    MULTIPLE_STATEMENTS = "multiple_statements"
    NON_QUERY_STATEMENT = "non_query_statement"
    PROHIBITED_FUNCTION = "prohibited_function"
    PROHIBITED_OPERATION = "prohibited_operation"
    SQL_TOO_LARGE = "sql_too_large"
    SYSTEM_SCHEMA = "system_schema"
    TABLE_NOT_ALLOWED = "table_not_allowed"


class SqlPolicyResult(FrozenModel):
    """Only safe policy facts; parsed syntax and source SQL never escape."""

    parse_valid: bool
    allowed: bool
    reason_code: SafeCode


def _contains_comment(sql: str) -> bool:
    """Detect PostgreSQL comments while ignoring quoted string contents."""
    index = 0
    length = len(sql)
    state = "normal"
    dollar_delimiter = ""
    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""
        if state == "normal":
            if char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif (char == "-" and following == "-") or (
                char == "/" and following == "*"
            ):
                return True
            elif char == "$":
                match = _DOLLAR_QUOTE.match(sql, index)
                if match is not None:
                    dollar_delimiter = match.group(0)
                    state = "dollar"
                    index = match.end() - 1
        elif state == "single":
            if char == "\\":
                index += 1
            elif char == "'":
                if following == "'":
                    index += 1
                else:
                    state = "normal"
        elif state == "double":
            if char == '"':
                if following == '"':
                    index += 1
                else:
                    state = "normal"
        elif sql.startswith(dollar_delimiter, index):
            state = "normal"
            index += len(dollar_delimiter) - 1
        index += 1
    return False


def _identifier_name(identifier: exp.Identifier | str | None) -> str:
    if identifier is None:
        return ""
    if isinstance(identifier, str):
        return identifier
    return identifier.name


def _normalized_table_part(table: exp.Table, part: str) -> str:
    value = table.args.get(part)
    name = _identifier_name(value)
    if isinstance(value, exp.Identifier) and value.args.get("quoted"):
        return name
    return name.casefold()


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return function.name.casefold()
    sql_name = function.sql_name()
    if sql_name == "ANONYMOUS":
        sql_name = function.name
    return sql_name.casefold()


class PostgresSqlPolicy:
    """Apply a deterministic allowlist policy to PostgreSQL query syntax."""

    def __init__(
        self,
        *,
        allowed_tables: frozenset[str] = DEFAULT_ALLOWED_TABLES,
        max_sql_bytes: int = DEFAULT_MAX_SQL_BYTES,
    ) -> None:
        if not allowed_tables:
            raise ValueError("SQL policy requires at least one allowed table")
        if max_sql_bytes < 1:
            raise ValueError("SQL byte limit must be positive")
        normalized_tables = frozenset(table.casefold() for table in allowed_tables)
        if any(not table.isidentifier() for table in normalized_tables):
            raise ValueError("allowed table names must be identifiers")
        self._allowed_tables = normalized_tables
        self._max_sql_bytes = max_sql_bytes

    @property
    def semantic_config(self) -> dict[str, object]:
        """Return public configuration covered by the evaluator digest."""
        return {
            "allowed_schemas": sorted(_ALLOWED_SCHEMAS),
            "allowed_tables": sorted(self._allowed_tables),
            "allowed_functions": sorted(_ALLOWED_FUNCTIONS),
            "dialect": POSTGRES_DIALECT,
            "max_sql_bytes": self._max_sql_bytes,
            "prohibited_functions": sorted(_PROHIBITED_FUNCTIONS),
            "prohibited_function_prefixes": list(_PROHIBITED_FUNCTION_PREFIXES),
            "prohibited_nodes": sorted(_PROHIBITED_NODE_NAMES),
            "policy_schema": "postgres-read-only/v1",
        }

    def evaluate(self, sql: str) -> SqlPolicyResult:
        """Return content-free syntax and safety evidence for one SQL string."""
        if len(sql.encode("utf-8")) > self._max_sql_bytes:
            return self._result(False, False, SqlPolicyReason.SQL_TOO_LARGE)
        try:
            statements = sqlglot.parse(sql, read=POSTGRES_DIALECT)
        except Exception:
            return self._result(False, False, SqlPolicyReason.INVALID_SQL)
        if not statements or any(statement is None for statement in statements):
            return self._result(False, False, SqlPolicyReason.INVALID_SQL)
        if len(statements) != 1:
            return self._result(True, False, SqlPolicyReason.MULTIPLE_STATEMENTS)

        statement = statements[0]
        if _contains_comment(sql):
            return self._result(True, False, SqlPolicyReason.COMMENTS_NOT_ALLOWED)
        if not isinstance(statement, exp.Query):
            return self._result(True, False, SqlPolicyReason.NON_QUERY_STATEMENT)

        nodes = tuple(statement.walk())
        if any(type(node).__name__ in _PROHIBITED_NODE_NAMES for node in nodes):
            return self._result(True, False, SqlPolicyReason.PROHIBITED_OPERATION)

        cte_names = {
            cte.alias_or_name.casefold()
            for cte in statement.find_all(exp.CTE)
            if cte.alias_or_name
        }
        for table in statement.find_all(exp.Table):
            name = _normalized_table_part(table, "this")
            schema = _normalized_table_part(table, "db")
            catalog = _normalized_table_part(table, "catalog")
            if not schema and not catalog and name.casefold() in cte_names:
                continue
            if schema in _SYSTEM_SCHEMAS or catalog in _SYSTEM_SCHEMAS:
                return self._result(True, False, SqlPolicyReason.SYSTEM_SCHEMA)
            if catalog or schema not in _ALLOWED_SCHEMAS:
                return self._result(True, False, SqlPolicyReason.TABLE_NOT_ALLOWED)
            if name not in self._allowed_tables:
                return self._result(True, False, SqlPolicyReason.TABLE_NOT_ALLOWED)

        for function in statement.find_all(exp.Func):
            name = _function_name(function)
            if name in _PROHIBITED_FUNCTIONS or name.startswith(
                _PROHIBITED_FUNCTION_PREFIXES
            ):
                return self._result(True, False, SqlPolicyReason.PROHIBITED_FUNCTION)
            if name not in _ALLOWED_FUNCTIONS:
                return self._result(True, False, SqlPolicyReason.PROHIBITED_FUNCTION)
        return self._result(True, True, SqlPolicyReason.ALLOWED)

    @staticmethod
    def _result(
        parse_valid: bool,
        allowed: bool,
        reason: SqlPolicyReason,
    ) -> SqlPolicyResult:
        return SqlPolicyResult(
            parse_valid=parse_valid,
            allowed=allowed,
            reason_code=reason.value,
        )
