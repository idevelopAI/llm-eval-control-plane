from math import nan

from pydantic import ValidationError
from pytest import raises

from llm_eval_control_plane.domain import CanonicalJson
from llm_eval_control_plane.domain.sql import (
    SqlBehavior,
    SqlExpectation,
    SqlReplayResult,
    SqlResultOrder,
    SqlTargetOutput,
)


def query_expectation() -> SqlExpectation:
    return SqlExpectation(
        behavior=SqlBehavior.QUERY,
        reference_sql="SELECT id FROM employees ORDER BY id",
        expected_columns=("id",),
        expected_rows=((1,), (2,)),
        result_order=SqlResultOrder.ORDERED,
    )


def test_query_expectation_round_trips_through_canonical_json() -> None:
    expected = query_expectation()

    restored = SqlExpectation.from_canonical(expected.to_canonical())

    assert restored == expected
    assert restored.to_canonical().to_value() == {
        "behavior": "query",
        "expected_columns": ["id"],
        "expected_rows": [[1], [2]],
        "reference_sql": "SELECT id FROM employees ORDER BY id",
        "result_order": "ordered",
        "schema_version": "1",
    }


def test_expectation_requires_exact_category_specific_fields() -> None:
    with raises(ValidationError, match="complete SQL evidence"):
        SqlExpectation(behavior=SqlBehavior.QUERY)
    with raises(ValidationError, match="cannot accept clarification"):
        SqlExpectation(
            behavior=SqlBehavior.QUERY,
            reference_sql="SELECT id FROM employees",
            expected_columns=("id",),
            expected_rows=((1,),),
            result_order=SqlResultOrder.ORDERED,
            accepted_clarification_codes=("provider_clarification",),
        )
    with raises(ValidationError, match="accepted codes"):
        SqlExpectation(behavior=SqlBehavior.CLARIFICATION)
    with raises(ValidationError, match="cannot contain SQL"):
        SqlExpectation(
            behavior=SqlBehavior.CLARIFICATION,
            reference_sql="SELECT 1",
            accepted_clarification_codes=("provider_clarification",),
        )
    with raises(ValidationError, match="cannot accept clarification"):
        SqlExpectation(
            behavior=SqlBehavior.REFUSAL,
            accepted_clarification_codes=("provider_clarification",),
        )


def test_clarification_codes_are_unique_sorted_and_refusal_is_minimal() -> None:
    clarification = SqlExpectation(
        behavior=SqlBehavior.CLARIFICATION,
        accepted_clarification_codes=("provider_clarification", "ambiguity"),
    )
    refusal = SqlExpectation(behavior=SqlBehavior.REFUSAL)

    assert clarification.accepted_clarification_codes == (
        "ambiguity",
        "provider_clarification",
    )
    assert refusal.to_canonical().to_value() == {
        "behavior": "refusal",
        "schema_version": "1",
    }
    with raises(ValidationError, match="must be unique"):
        SqlExpectation(
            behavior=SqlBehavior.CLARIFICATION,
            accepted_clarification_codes=("ambiguity", "ambiguity"),
        )


def test_query_rows_match_unique_expected_columns() -> None:
    with raises(ValidationError, match="column names must be unique"):
        SqlExpectation(
            behavior=SqlBehavior.QUERY,
            reference_sql="SELECT id, id FROM employees",
            expected_columns=("id", "id"),
            expected_rows=((1, 1),),
            result_order=SqlResultOrder.ORDERED,
        )
    with raises(ValidationError, match="row width"):
        SqlExpectation(
            behavior=SqlBehavior.QUERY,
            reference_sql="SELECT id FROM employees",
            expected_columns=("id",),
            expected_rows=((1, 2),),
            result_order=SqlResultOrder.ORDERED,
        )


def test_sql_contracts_are_strict_and_forbid_unknown_fields() -> None:
    document = CanonicalJson.from_value(
        {
            "behavior": "query",
            "expected_columns": ["id"],
            "expected_rows": [["1"]],
            "reference_sql": "SELECT id FROM employees",
            "result_order": "ordered",
            "schema_version": "1",
        }
    )
    assert SqlExpectation.from_canonical(document).expected_rows == (("1",),)

    value = document.to_value()
    assert isinstance(value, dict)
    with raises(ValidationError):
        SqlExpectation.from_canonical(
            CanonicalJson.from_value(
                {
                    **value,
                    "unknown": True,
                }
            )
        )
    with raises(ValidationError):
        SqlExpectation(
            behavior="query",  # type: ignore[arg-type]
            reference_sql="SELECT id FROM employees",
            expected_columns=("id",),
            expected_rows=((1,),),
            result_order=SqlResultOrder.ORDERED,
        )


def test_target_output_enforces_minimal_query_clarification_and_refusal() -> None:
    query = SqlTargetOutput(
        kind=SqlBehavior.QUERY,
        sql_executions=("SELECT id FROM employees",),
    )
    clarification = SqlTargetOutput(
        kind=SqlBehavior.CLARIFICATION,
        clarification_code="provider_clarification",
    )
    refusal = SqlTargetOutput(kind=SqlBehavior.REFUSAL)

    assert SqlTargetOutput.from_canonical(query.to_canonical()) == query
    assert clarification.to_canonical().to_value() == {
        "clarification_code": "provider_clarification",
        "kind": "clarification",
        "schema_version": "1",
    }
    assert refusal.to_canonical().to_value() == {
        "kind": "refusal",
        "schema_version": "1",
    }
    with raises(ValidationError, match="require SQL"):
        SqlTargetOutput(kind=SqlBehavior.QUERY)
    with raises(ValidationError, match="cannot include SQL"):
        SqlTargetOutput(
            kind=SqlBehavior.CLARIFICATION,
            clarification_code="provider_clarification",
            sql_executions=("SELECT 1",),
        )
    with raises(ValidationError, match="cannot include category"):
        SqlTargetOutput(
            kind=SqlBehavior.REFUSAL,
            clarification_code="provider_clarification",
        )


def test_irrelevant_sql_fields_are_rejected_even_when_explicitly_null() -> None:
    with raises(ValidationError, match="cannot contain SQL"):
        SqlExpectation.from_canonical(
            CanonicalJson.from_value(
                {
                    "behavior": "refusal",
                    "reference_sql": None,
                    "schema_version": "1",
                }
            )
        )
    with raises(ValidationError, match="cannot include category"):
        SqlTargetOutput.from_canonical(
            CanonicalJson.from_value(
                {
                    "kind": "refusal",
                    "schema_version": "1",
                    "sql_executions": None,
                }
            )
        )


def test_repr_never_contains_sql_or_rows() -> None:
    expected = query_expectation()
    output = SqlTargetOutput(
        kind=SqlBehavior.QUERY,
        sql_executions=("SELECT secret FROM employees",),
    )
    replay = SqlReplayResult(columns=("secret",), rows=(("classified",),))

    assert "SELECT" not in repr(expected)
    assert "SELECT" not in repr(output)
    assert "classified" not in repr(replay)


def test_replay_result_rejects_duplicate_columns_and_nonfinite_values() -> None:
    with raises(ValidationError, match="column names must be unique"):
        SqlReplayResult(columns=("id", "id"), rows=((1, 1),))
    with raises(ValidationError):
        SqlReplayResult(columns=("value",), rows=((nan,),))
