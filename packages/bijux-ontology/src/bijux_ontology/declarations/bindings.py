"""Caller-owned application identities and evidence for finite checked bindings."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report


@dataclass(frozen=True)
class BindingIdentity:
    """An explicit owner and field/output path, without inferred business policy."""

    owner: str
    path: tuple[str | int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise InputError("A binding requires a nonempty application owner")
        if type(self.path) is not tuple or not self.path:
            raise InputError("A binding requires a nonempty immutable path")
        if any(
            not (isinstance(part, str) and part or type(part) is int and part >= 0)
            for part in self.path
        ):
            raise InputError(
                "Binding paths require nonempty keys or nonnegative indexes"
            )


@dataclass(frozen=True)
class BindingObservation:
    """One actual checked association; exclusions retain their explicit reason.

    A negative completed comparison still establishes that this identity was
    exercised. Unsupported, absent or excluded comparisons cannot satisfy a
    required binding, regardless of policy acceptance of their report.
    """

    identity: BindingIdentity
    report: Report
    exclusion: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BindingIdentity) or not isinstance(
            self.report, Report
        ):
            raise InputError("Binding observations require typed identity and evidence")
        if self.exclusion is not None and (
            not isinstance(self.exclusion, str) or not self.exclusion.strip()
        ):
            raise InputError("A binding exclusion requires a nonempty reason")

    @property
    def completed(self) -> bool:
        """Require a real completed comparison, independently of truth or policy."""
        return (
            self.exclusion is None
            and self.report.checked > 0
            and self.report.complete
            and self.report.outcome in ("agreement", "disagreement")
        )


@dataclass(frozen=True)
class ContractEvidence:
    """A raw operation report with its actual finite binding observations."""

    report: Report
    bindings: tuple[BindingObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.report, Report) or type(self.bindings) is not tuple:
            raise InputError(
                "Contract evidence requires a Report and immutable bindings"
            )
        if any(not isinstance(item, BindingObservation) for item in self.bindings):
            raise InputError("Expected typed binding observations")
