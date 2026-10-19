"""Explicit mapping interpretations and original-row logical evidence."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.types import MappingSet

from ..axioms.changes.types import AxiomCandidate
from ..axioms.types import AxiomInspection
from ..explanations.types import ExplanationCheck, SourceAxiom
from ..integration.types import TheoryComparison, TheoryComponent, TheoryObservation

BridgeKind = Literal[
    "equivalent_classes",
    "subclass",
    "superclass",
    "equivalent_object_properties",
    "sub_object_property",
    "super_object_property",
    "equivalent_data_properties",
    "sub_data_property",
    "super_data_property",
]


@dataclass(frozen=True)
class BridgeInterpretation:
    """A caller-approved logical meaning, never inferred from mapping confidence."""

    occurrence: MappingOccurrence
    kind: BridgeKind
    reason: str


@dataclass(frozen=True)
class MappingBridge:
    """One original mapping occurrence and its checked native bridge module."""

    interpretation: BridgeInterpretation
    candidate: AxiomCandidate
    module: TheoryComponent | None


@dataclass(frozen=True)
class BridgeAttribution:
    """Reproduced support and candidate rows, without unique or global blame."""

    explanation: ExplanationCheck
    origins: tuple[SourceAxiom, ...]
    interpretations: tuple[BridgeInterpretation, ...]


@dataclass(frozen=True)
class MappingBridgeCheck:
    """Selected logical compatibility with independent original/union baselines.

    Raw original answers and partial current work remain inspectable. Completion
    covers only supplied interpretations, classes and protected entailments.
    Unselected rows remain mapping records and have no implicit OWL meaning.
    """

    mappings: MappingSet | None
    interpretations: tuple[BridgeInterpretation, ...]
    role_inspections: tuple[AxiomInspection, ...]
    bridges: tuple[MappingBridge, ...]
    observations: tuple[TheoryObservation, ...]
    component_comparison: TheoryComparison | None
    bridge_comparison: TheoryComparison | None
    explanations: tuple[ExplanationCheck, ...]
    attributions: tuple[BridgeAttribution, ...]
    scope_complete: bool
    report: Report
