"""Offline target with explicit deterministic scenarios for CI and demos."""

from __future__ import annotations

from typing import Final

from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    TargetOutcome,
    TargetRequest,
    TargetResponse,
    TokenUsage,
    sha256_digest,
)
from llm_eval_control_plane.domain.canonical import JsonValue


class FakeTargetError(RuntimeError):
    """A safe configured fake-target failure with a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__("Deterministic fake target could not execute the scenario")
        self.code = code


class DeterministicFakeTarget:
    """Execute named scenarios without network, credentials, or randomness."""

    _BEHAVIOR_VERSION: Final = "fake-target/v1"

    def __init__(self, *, name: str = "fake/deterministic", revision: int = 1) -> None:
        self._ref = ArtifactRef(
            kind=ArtifactKind.TARGET,
            name=name,
            revision=revision,
            digest=sha256_digest(
                {
                    "behavior": self._BEHAVIOR_VERSION,
                    "scenarios": [
                        "echo",
                        "uppercase",
                        "offset",
                        "refuse",
                        "mismatch",
                        "malformed",
                        "missing_usage",
                        "raise",
                    ],
                }
            ),
        )
        self._invocations: list[str] = []

    @property
    def ref(self) -> ArtifactRef:
        return self._ref

    @property
    def invocations(self) -> tuple[str, ...]:
        return tuple(self._invocations)

    async def invoke(self, request: TargetRequest) -> object:
        """Return one validated or deliberately malformed target envelope."""
        self._invocations.append(request.case_id)
        payload = request.input.to_value()
        if not isinstance(payload, dict):
            raise FakeTargetError("invalid_input")
        scenario = payload.get("scenario")
        if not isinstance(scenario, str):
            raise FakeTargetError("missing_scenario")

        if scenario == "malformed":
            return {
                "output": {"canonical": '"private-sentinel"'},
                "usage": {"input_units": -1, "output_units": 0},
            }
        if scenario == "missing_usage":
            return {
                "output": {"canonical": '"answer"'},
                "outcome": "completed",
            }
        if scenario == "raise":
            raise FakeTargetError("configured_failure")

        output, outcome, refusal_code = self._execute_scenario(scenario, payload)
        output_document = CanonicalJson.from_value(output)
        return TargetResponse(
            output=output_document,
            outcome=outcome,
            refusal_code=refusal_code,
            usage=TokenUsage(
                input_units=self._estimated_units(request.input.to_bytes()),
                output_units=self._estimated_units(output_document.to_bytes()),
            ),
        )

    def _execute_scenario(
        self,
        scenario: str,
        payload: dict[str, JsonValue],
    ) -> tuple[JsonValue, TargetOutcome, str | None]:
        if scenario == "echo":
            return payload.get("value"), TargetOutcome.COMPLETED, None
        if scenario == "mismatch":
            return payload.get("actual"), TargetOutcome.COMPLETED, None
        if scenario == "uppercase":
            value = payload.get("value")
            if not isinstance(value, str):
                raise FakeTargetError("uppercase_requires_text")
            return value.upper(), TargetOutcome.COMPLETED, None
        if scenario == "offset":
            value = payload.get("value")
            offset = payload.get("offset")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or isinstance(offset, bool)
                or not isinstance(offset, (int, float))
            ):
                raise FakeTargetError("offset_requires_numbers")
            return value + offset, TargetOutcome.COMPLETED, None
        if scenario == "refuse":
            return (
                payload.get("value", "refused"),
                TargetOutcome.REFUSED,
                "policy_block",
            )
        raise FakeTargetError("unknown_scenario")

    @staticmethod
    def _estimated_units(value: bytes) -> int:
        return max(1, (len(value) + 3) // 4)
