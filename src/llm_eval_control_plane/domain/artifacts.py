"""References to immutable, versioned evaluation artifacts."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field, PositiveInt

from llm_eval_control_plane.domain.models import FrozenModel

ArtifactName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class ArtifactKind(StrEnum):
    """Kinds of versioned inputs required to reproduce an evaluation run."""

    DATASET = "dataset"
    TARGET = "target"
    PROMPT = "prompt"
    EVALUATOR = "evaluator"
    SUITE = "suite"
    GATE_POLICY = "gate_policy"


class ArtifactRef(FrozenModel):
    """Stable reference to one immutable artifact revision."""

    kind: ArtifactKind
    name: ArtifactName
    revision: PositiveInt
    digest: Sha256Digest | None = None

    @property
    def logical_key(self) -> tuple[ArtifactKind, str, int]:
        """Identify a revision independently from its optional digest assertion."""
        return (self.kind, self.name, self.revision)
