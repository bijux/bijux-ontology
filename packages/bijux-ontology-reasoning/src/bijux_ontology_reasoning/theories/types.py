"""Owned finite theory scopes and independent original/native observations."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.reports import Report

from ..axioms.types import ProjectedAxiom
from ..diagnosis.types import ConsistencyCheck
from ..documents.types import OwlDocument
from ..entailment.types import EntailmentCheck
from ..explanations.types import ExplanationCheck, SourceAxiom


@dataclass(frozen=True)
class TheoryComponent:
    """One separately interpreted original theory and its explicit offline imports."""

    label: str
    source: OwlDocument
    imports: Mapping[str, OwlDocument] | None = None


@dataclass(frozen=True)
class TheoryCombination:
    """An explicitly selected deployment, without implicit mapping interpretation."""

    label: str
    components: tuple[str, ...]
    bridges: tuple[str, ...] = ()


@dataclass(frozen=True)
class TheoryObservation:
    """Raw logical answers with separate execution and inventory coverage.

    report covers observation completion, not truth of every requested axiom.
    A complete not-entailed answer remains negative in entailments. Actual named
    class answers and absent class names remain distinct; no fresh-entity native
    answer is manufactured from signature absence.
    """

    label: str
    source: OwlDocument
    imports: tuple[tuple[str, OwlDocument], ...]
    consistency: ConsistencyCheck
    entailments: EntailmentCheck | None
    named_classes: tuple[URIRef, ...] | None
    consumer_entailments: tuple[ProjectedAxiom, ...]
    signature_complete: bool
    report: Report


@dataclass(frozen=True)
class EntailmentComparison:
    """Known false, known true and missing execution stay distinct per theory."""

    axiom: ProjectedAxiom
    before: tuple[bool | None, ...]
    after: bool | None


@dataclass(frozen=True)
class TheoryComparison:
    """Changes established only against the explicit qualified prior theories."""

    before: tuple[str, ...]
    after: str
    newly_inconsistent: bool | None
    newly_unsatisfiable: tuple[URIRef, ...]
    preexisting_unsatisfiable: tuple[URIRef, ...]
    entailments: tuple[EntailmentComparison, ...]
    report: Report


@dataclass(frozen=True)
class TheoryInteraction:
    """A reproduced failure and exact native source structures, without unique blame."""

    stage: Literal["unbridged", "bridged"]
    class_iri: URIRef | None
    explanation: ExplanationCheck
    origins: tuple[SourceAxiom, ...]


@dataclass(frozen=True)
class JointCompatibility:
    """Keep unbridged problems separate from additional bridge-induced failures."""

    selection: TheoryCombination
    unbridged: TheoryObservation
    joint: TheoryObservation
    component_comparison: TheoryComparison
    bridge_comparison: TheoryComparison | None
    interactions: tuple[TheoryInteraction, ...]
    report: Report


@dataclass(frozen=True)
class IntegrationCheck:
    """Compatibility of selected deployments, never every possible component subset.

    observations includes paid partial current work even when a complete joint
    result could not be assembled. scope_complete covers only the caller's
    selected combinations and explanation/contract obligations.
    """

    observations: tuple[TheoryObservation, ...]
    explanations: tuple[ExplanationCheck, ...]
    combinations: tuple[JointCompatibility, ...]
    requested_combinations: int
    scope_complete: bool
    report: Report
    all_combinations: bool = False
