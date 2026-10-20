"""Finite public signatures and explicit one-level provider bindings."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.owl.obligations import ObligationResult, OntologyObligation
from bijux_ontology.owl.relations.properties import PropertyContract
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView


@dataclass(frozen=True)
class ModulePropertyRequirement:
    """Require an effective named endpoint guarantee, not property nonemptiness.

    Domain/range constraints are conjunctive: one sufficient included endpoint
    establishes this selected guarantee. Disjoint datatype comparisons alone
    cannot disprove an OWL range entailment, because the property may be empty.
    """

    property: str
    position: Literal["domain", "range"]
    relation: Literal["subsumption", "datatype_inclusion"]
    required: str
    location: str = ""


@dataclass(frozen=True)
class ModuleRequirement:
    """One external public interface with independently selected proof questions."""

    name: str
    exports: tuple[TermDeclaration, ...]
    obligations: tuple[OntologyObligation, ...] = ()
    properties: tuple[ModulePropertyRequirement, ...] = ()


@dataclass(frozen=True)
class ModuleContract:
    """Named exports and explicit external requirements, without inferred owners."""

    name: str
    exports: tuple[TermDeclaration, ...]
    requires: tuple[ModuleRequirement, ...] = ()


@dataclass(frozen=True)
class ModuleProvider:
    """An explicit public inventory backed by an actual selected owned view.

    Only local/import roles supply provider assertions. Its contract's own
    requirements are not recursively resolved by another consumer's check.
    """

    contract: ModuleContract
    view: VocabularyView


@dataclass(frozen=True)
class ModuleBinding:
    """Bind one consumer requirement name to one explicit provider name."""

    requirement: str
    provider: str


@dataclass(frozen=True)
class ModuleRequirementResult:
    """Original bound scopes, native proofs and the exact selected interface.

    Property comparison results retain their independent datatype/class truth;
    the report qualifies their use as a property guarantee. A contradicted
    datatype inclusion is not automatically a contradicted property entailment.
    """

    requirement: ModuleRequirement
    bindings: tuple[ModuleBinding, ...]
    providers: tuple[ModuleProvider, ...]
    obligations: tuple[ObligationResult, ...]
    property_contracts: tuple[tuple[ModulePropertyRequirement, PropertyContract], ...]
    report: Report


@dataclass(frozen=True)
class ModuleWiring:
    """One-level interface evidence; never whole-provider equivalence or consistency."""

    consumer: ModuleContract
    requirements: tuple[ModuleRequirementResult, ...]
    report: Report
