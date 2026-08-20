import asyncio

from pytest import mark, raises

from llm_eval_control_plane.adapters import DeterministicFakeTarget, FakeTargetError
from llm_eval_control_plane.domain import (
    ArtifactKind,
    CanonicalJson,
    TargetOutcome,
    TargetRequest,
    TargetResponse,
)


def request(case_id: str, scenario: str, **values: object) -> TargetRequest:
    return TargetRequest(
        case_id=case_id,
        input=CanonicalJson.from_value({"scenario": scenario, **values}),
    )


def invoke(target: DeterministicFakeTarget, item: TargetRequest) -> object:
    return asyncio.run(target.invoke(item))


@mark.parametrize(
    ("scenario", "values", "expected"),
    [
        ("echo", {"value": {"answer": 42}}, {"answer": 42}),
        ("uppercase", {"value": "Straße"}, "STRASSE"),
        ("offset", {"value": 1.0, "offset": 0.1}, 1.1),
        ("mismatch", {"actual": "wrong"}, "wrong"),
    ],
)
def test_fake_target_executes_known_scenarios_deterministically(
    scenario: str, values: dict[str, object], expected: object
) -> None:
    target = DeterministicFakeTarget()
    item = request("case-1", scenario, **values)

    first = invoke(target, item)
    second = invoke(target, item)

    assert isinstance(first, TargetResponse)
    assert first == second
    assert first.output.to_value() == expected
    assert first.usage.input_units > 0
    assert first.usage.output_units > 0
    assert target.invocations == ("case-1", "case-1")


def test_fake_target_returns_structured_refusal_independent_of_wording() -> None:
    result = invoke(
        DeterministicFakeTarget(),
        request("unsafe", "refuse", value="harmless-looking text"),
    )

    assert isinstance(result, TargetResponse)
    assert result.outcome is TargetOutcome.REFUSED
    assert result.refusal_code == "policy_block"


def test_fake_target_returns_deliberately_invalid_envelopes() -> None:
    target = DeterministicFakeTarget()

    malformed = invoke(target, request("bad", "malformed"))
    missing_usage = invoke(target, request("missing", "missing_usage"))

    assert isinstance(malformed, dict)
    assert malformed["usage"] == {"input_units": -1, "output_units": 0}
    assert isinstance(missing_usage, dict)
    assert "usage" not in missing_usage


@mark.parametrize(
    ("item", "code"),
    [
        (
            TargetRequest(case_id="scalar", input=CanonicalJson.from_value("value")),
            "invalid_input",
        ),
        (
            TargetRequest(case_id="missing", input=CanonicalJson.from_value({})),
            "missing_scenario",
        ),
        (request("unknown", "unknown"), "unknown_scenario"),
        (request("raise", "raise"), "configured_failure"),
        (
            request("uppercase", "uppercase", value=1),
            "uppercase_requires_text",
        ),
        (request("offset", "offset", value=True, offset=1), "offset_requires_numbers"),
    ],
)
def test_fake_target_raises_safe_typed_scenario_errors(
    item: TargetRequest, code: str
) -> None:
    target = DeterministicFakeTarget()

    with raises(FakeTargetError) as raised:
        invoke(target, item)

    assert raised.value.code == code
    assert "private" not in str(raised.value).lower()


def test_fake_target_has_resolved_version_identity_and_fresh_output_values() -> None:
    target = DeterministicFakeTarget(revision=3)
    item = request("nested", "echo", value={"items": [1, 2]})

    first = invoke(target, item)
    assert isinstance(first, TargetResponse)
    mutable_output = first.output.to_value()
    assert isinstance(mutable_output, dict)
    mutable_output["items"] = []

    second = invoke(target, item)
    assert isinstance(second, TargetResponse)
    assert second.output.to_value() == {"items": [1, 2]}
    assert target.ref.kind is ArtifactKind.TARGET
    assert target.ref.revision == 3
    assert target.ref.digest is not None
