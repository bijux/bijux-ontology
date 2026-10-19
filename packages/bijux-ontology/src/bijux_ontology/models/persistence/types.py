"""Typed inert preparation receipts; validation results are never cache entries."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.models.contracts.preparation import PreparedModels


def _digest(value: str) -> None:
    """Require an exact SHA256 spelling rather than an ambiguous cache key."""
    if (
        type(value) is not str
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise InputError("Preparation fingerprints require lowercase SHA256")


@dataclass(frozen=True, slots=True)
class PreparationIdentity:
    """Exact explicit current input and installed semantic engine fingerprints."""

    inputs_sha256: str
    engine_sha256: str

    def __post_init__(self) -> None:
        """Reject invalid identity fields before comparison or report formatting."""
        _digest(self.inputs_sha256)
        _digest(self.engine_sha256)


@dataclass(frozen=True, slots=True)
class ModelPreparationArtifact:
    """Immutable compiler output with an externally transferable integrity pin."""

    content: bytes | None
    sha256: str | None
    identity: PreparationIdentity | None
    report: Report


@dataclass(frozen=True, slots=True)
class PreparationReuse:
    """Only the source roles and native inspection selections actually adopted."""

    sources: tuple[str, ...] = ()
    inspections: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class RestoredModels:
    """A newly checked plan or unavailable preparation, with fresh load evidence."""

    prepared: PreparedModels | None
    report: Report
    reused: PreparationReuse = PreparationReuse()
