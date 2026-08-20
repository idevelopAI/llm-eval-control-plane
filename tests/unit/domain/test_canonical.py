import math

from pydantic import ValidationError
from pytest import mark, raises

from llm_eval_control_plane.domain import (
    CanonicalJson,
    CanonicalJsonError,
    canonical_json_bytes,
    parse_json,
    sha256_digest,
)


def test_rfc8785_canonical_bytes_and_digest_are_stable() -> None:
    value = {"z": [3, 2, 1], "a": "Straße", "number": 1.0}

    assert canonical_json_bytes(value) == (
        b'{"a":"Stra\xc3\x9fe","number":1,"z":[3,2,1]}'
    )
    assert sha256_digest(value) == (
        "sha256:60f4e10c25bf7b88958bbf7faa5040bb6808a52094d7a74b1273d83cd1ccbee3"
    )


def test_parse_json_rejects_duplicate_keys_without_echoing_them() -> None:
    sentinel = "private-secret-value"

    with raises(CanonicalJsonError) as raised:
        parse_json(f'{{"{sentinel}": 1, "{sentinel}": 2}}')

    assert raised.value.code == "duplicate_key"
    assert sentinel not in str(raised.value)


@mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parse_json_rejects_non_finite_numbers(constant: str) -> None:
    with raises(CanonicalJsonError) as raised:
        parse_json(constant)

    assert raised.value.code == "non_finite_number"


def test_parse_json_rejects_bom_and_malformed_json() -> None:
    with raises(CanonicalJsonError, match="must not start with a BOM"):
        parse_json('\ufeff{"ok":true}')
    with raises(CanonicalJsonError, match="Could not parse JSON"):
        parse_json("not-json")


@mark.parametrize(
    "value",
    [math.inf, math.nan, 2**60, {1: "not-a-string-key"}, object()],
)
def test_canonical_json_rejects_values_outside_jcs(value: object) -> None:
    with raises(CanonicalJsonError):
        canonical_json_bytes(value)


def test_canonical_document_round_trips_without_sharing_mutable_state() -> None:
    source = {"nested": [1, {"ok": True}]}
    document = CanonicalJson.from_value(source)

    restored = document.to_value()
    assert restored == source
    assert restored is not source
    assert CanonicalJson.from_json(' { "nested" : [1, {"ok": true}] } ') == document
    assert document.to_bytes() == b'{"nested":[1,{"ok":true}]}'


def test_canonical_document_requires_canonical_text_and_is_frozen() -> None:
    with raises(ValidationError, match="must use RFC 8785 canonical form"):
        CanonicalJson(canonical='{ "value": 1 }')

    document = CanonicalJson.from_value("unchanged")
    field_name = "canonical"
    with raises(ValidationError, match="Instance is frozen"):
        setattr(document, field_name, '"changed"')
