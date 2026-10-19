"""Explicit sequential migration operations and retained execution evidence."""

from collections.abc import Callable
from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate, MigrationCheck
from bijux_ontology.rdf.records.types import SnapshotWitness
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True)
class MigrationStep:
    """One trusted operation with required preconditions and postconditions.

    The callback receives the current owned snapshot and cumulative budget. It
    must return an ordinary checked migration candidate, including its exact
    patch. In-process callbacks retain their own Python execution boundary.
    """

    name: str
    transform: Callable[[PreparedSource, Budget], MigrationCandidate]
    preconditions: tuple[MigrationCheck, ...]
    postconditions: tuple[MigrationCheck, ...]

    def __post_init__(self) -> None:
        """Qualify stable operation identity without executing consumer code."""
        bound_text(self.name, Budget())
        if not self.name or not callable(self.transform):
            raise InputError("Migration operations require a name and callable")
        for checks in (self.preconditions, self.postconditions):
            if type(checks) is not tuple or any(
                not isinstance(check, MigrationCheck) for check in checks
            ):
                raise InputError("Migration conditions require immutable check tuples")


@dataclass(frozen=True)
class MigrationStepResult:
    """One attempted operation, retaining native patches and gate evidence."""

    name: str
    candidate: MigrationCandidate | None
    report: Report


@dataclass(frozen=True, init=False)
class MigrationPlanResult:
    """A dry run; final source exists only after every required gate completes."""

    source: PreparedSource | None
    steps: tuple[MigrationStepResult, ...]
    report: Report

    _witness: SnapshotWitness | None

    def __init__(self) -> None:
        """Only plan execution may associate final truth with an owned witness."""
        raise InputError("Migration results must come from execute_migration_plan")

    @classmethod
    def _from_execution(
        cls,
        source: PreparedSource | None,
        steps: tuple[MigrationStepResult, ...],
        report: Report,
        witness: SnapshotWitness | None,
    ) -> "MigrationPlanResult":
        """Retain the already budgeted final snapshot without public rebinding."""
        result = object.__new__(cls)
        for name, value in (
            ("source", source),
            ("steps", steps),
            ("report", report),
            ("_witness", witness),
        ):
            object.__setattr__(result, name, value)
        return result
