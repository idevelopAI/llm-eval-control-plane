"""RFC 8785 canonical JSON values used by immutable domain contracts."""

from __future__ import annotations

import hashlib
import json
from typing import TypeAlias

import rfc8785
from pydantic import Field, field_validator

from llm_eval_control_plane.domain.models import FrozenModel

JsonValue: TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)


class CanonicalJsonError(ValueError):
    """Describe invalid JSON without retaining or echoing its contents."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.line = line
        self.column = column


def _reject_constant(_value: str) -> None:
    raise CanonicalJsonError(
        "non_finite_number",
        "JSON numbers must be finite",
    )


def _reject_duplicate_keys(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJsonError(
                "duplicate_key",
                "JSON objects must not contain duplicate keys",
            )
        result[key] = value
    return result


def _validated_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, list):
        return [_validated_json_value(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError(
                    "non_string_key",
                    "JSON object keys must be strings",
                )
            normalized[key] = _validated_json_value(item)
        return normalized
    raise CanonicalJsonError(
        "unsupported_type",
        "Value contains a type that JSON cannot represent",
    )


def parse_json(text: str) -> JsonValue:
    """Parse strict JSON while rejecting duplicate keys and non-finite numbers."""
    if text.startswith("\ufeff"):
        raise CanonicalJsonError("bom", "UTF-8 JSON must not start with a BOM")
    try:
        value: object = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except CanonicalJsonError:
        raise
    except json.JSONDecodeError as error:
        raise CanonicalJsonError(
            "invalid_json",
            "Could not parse JSON",
            line=error.lineno,
            column=error.colno,
        ) from error
    return _validated_json_value(value)


def canonical_json_bytes(value: object) -> bytes:
    """Return RFC 8785/JCS bytes for one JSON-compatible value."""
    normalized = _validated_json_value(value)
    try:
        return rfc8785.dumps(normalized)
    except (rfc8785.CanonicalizationError, UnicodeEncodeError) as error:
        raise CanonicalJsonError(
            "outside_jcs_domain",
            "Value is outside the RFC 8785 canonical JSON domain",
        ) from error


def sha256_digest(value: object) -> str:
    """Hash an RFC 8785 canonical value using the public digest format."""
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


class CanonicalJson(FrozenModel):
    """Deeply immutable JSON stored only in its canonical text representation."""

    canonical: str = Field(min_length=1, repr=False)

    @field_validator("canonical")
    @classmethod
    def require_canonical_text(cls, value: str) -> str:
        parsed = parse_json(value)
        canonical = canonical_json_bytes(parsed).decode("utf-8")
        if value != canonical:
            raise ValueError("JSON text must use RFC 8785 canonical form")
        return value

    @classmethod
    def from_value(cls, value: object) -> CanonicalJson:
        """Create a deeply immutable document from a JSON-compatible value."""
        return cls(canonical=canonical_json_bytes(value).decode("utf-8"))

    @classmethod
    def from_json(cls, text: str) -> CanonicalJson:
        """Parse arbitrary strict JSON and store its canonical representation."""
        return cls.from_value(parse_json(text))

    def to_value(self) -> JsonValue:
        """Return a fresh mutable JSON value for an adapter boundary."""
        return parse_json(self.canonical)

    def to_bytes(self) -> bytes:
        """Return the immutable UTF-8 representation."""
        return self.canonical.encode("utf-8")
