"""Finite stable consumer identities and raw logical upgrade observations."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.reports import Report

from ...axioms.types import ProjectedAxiom
from ...documents.types import OwlDocument
from ...inference.consistency.types import ConsistencyCheck
from ...inference.entailment.types import EntailmentCheck
from ...inference.explanations.types import ExplanationCheck
from ...inference.model_contracts.types import ModelCompatibility
from ...inference.relationships.types import InferredContracts, RelationshipValue


@dataclass(frozen=True)
class EntailmentContract:
    """One stable consumer obligation, separate from ontology entity spelling."""

    identifier: str
    axiom: ProjectedAxiom
    location: str = ""


@dataclass(frozen=True)
class SatisfiabilityContract:
    """A named class that the consumer requires to remain possible."""

    identifier: str
    class_iri: str
    location: str = ""


@dataclass(frozen=True)
class ModelContract:
    """One stable application contract owning its complete nested registry."""

    identifier: str
    models: tuple[ModelDeclaration, ...]
    location: str = ""


@dataclass(frozen=True)
class SeparationContract:
    """Allow aliases within groups while forbidding cross-group equivalence.

    All selected entities have the same explicit role. This consumer policy is
    separate from ontology consistency and ignores unrelated equivalent names.
    """

    identifier: str
    groups: tuple[tuple[RelationshipValue, ...], ...]
    location: str = ""


@dataclass(frozen=True)
class LogicalSnapshot:
    """Explicit finite obligations; no serializer or universal coverage is implied."""

    entailments: tuple[EntailmentContract, ...] = ()
    satisfiability: tuple[SatisfiabilityContract, ...] = ()
    models: tuple[ModelContract, ...] = ()
    separate: tuple[SeparationContract, ...] = ()


LogicalContract: TypeAlias = (
    EntailmentContract | SatisfiabilityContract | ModelContract | SeparationContract
)
LogicalCheck: TypeAlias = (
    ConsistencyCheck | EntailmentCheck | ModelCompatibility | InferredContracts
)
TransitionKind = Literal[
    "regression", "persistent_prior_failure", "improvement", "preserved", "unknown"
]


@dataclass(frozen=True)
class LogicalObservation:
    """A qualified consumer state alongside the unchanged underlying raw check."""

    identifier: str
    passed: bool | None
    check: LogicalCheck


@dataclass(frozen=True)
class LogicalSide:
    """Owned native interpretation and consumer snapshot, including partial work."""

    source: OwlDocument
    imports: tuple[tuple[str, OwlDocument], ...]
    consumer: LogicalSnapshot
    observations: tuple[LogicalObservation, ...]


@dataclass(frozen=True)
class LogicalTransition:
    """Match only explicit stable IDs; changes to the obligation remain visible."""

    identifier: str
    before: LogicalObservation | None
    after: LogicalObservation | None
    kind: TransitionKind
    contract_changed: bool | None


@dataclass(frozen=True)
class UpgradeExplanation:
    """Actual support for a selected observation, never a proof of absence."""

    identifier: str
    side: Literal["before", "after"]
    check: ExplanationCheck


@dataclass(frozen=True)
class LogicalUpgrade:
    """Finite transition qualification with raw before/after truth preserved.

    The evidence report retains prior findings, so agreement is not the upgrade
    compatibility verdict. Compatible=True covers only the supplied matched
    consumer snapshots, including explicitly recorded declaration changes.
    Missing evidence or incomplete selected explanations prevents True; a
    proved regression remains False even when explanation work stops.
    """

    before: LogicalSide | None
    after: LogicalSide | None
    transitions: tuple[LogicalTransition, ...]
    explanations: tuple[UpgradeExplanation, ...]
    compatible: bool | None
    report: Report
