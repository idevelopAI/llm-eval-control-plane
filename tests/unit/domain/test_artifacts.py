from pydantic import ValidationError
from pytest import raises

from llm_eval_control_plane.domain import ArtifactKind, ArtifactRef


def test_artifact_reference_round_trips_through_json() -> None:
    artifact = ArtifactRef(
        kind=ArtifactKind.DATASET,
        name="databridge/bilingual-sql",
        revision=3,
        digest="sha256:" + "a" * 64,
    )

    restored = ArtifactRef.model_validate_json(artifact.model_dump_json())

    assert restored == artifact
    assert hash(restored) == hash(artifact)


def test_artifact_reference_rejects_unknown_fields() -> None:
    with raises(ValidationError, match="Extra inputs are not permitted"):
        ArtifactRef.model_validate(
            {
                "kind": "dataset",
                "name": "sample",
                "revision": 1,
                "secret": "must-not-be-stored",
            }
        )


def test_artifact_reference_rejects_invalid_identifier() -> None:
    with raises(ValidationError, match="String should match pattern"):
        ArtifactRef(kind=ArtifactKind.DATASET, name=" leading-space", revision=1)


def test_artifact_reference_rejects_non_positive_revision() -> None:
    with raises(ValidationError, match="greater than 0"):
        ArtifactRef(kind=ArtifactKind.DATASET, name="sample", revision=0)


def test_artifact_reference_rejects_noncanonical_digest() -> None:
    with raises(ValidationError, match="String should match pattern"):
        ArtifactRef(
            kind=ArtifactKind.DATASET,
            name="sample",
            revision=1,
            digest="A" * 64,
        )


def test_artifact_reference_is_frozen() -> None:
    artifact = ArtifactRef(kind=ArtifactKind.DATASET, name="sample", revision=1)
    field_name = "revision"

    with raises(ValidationError, match="Instance is frozen"):
        setattr(artifact, field_name, 2)
