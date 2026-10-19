"""Finite public signatures and explicit one-level provider bindings."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.owl.modules import contracts
from bijux_ontology.owl.obligations import ObligationResult
from bijux_ontology.owl.relations.properties import PropertyContract
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView


@dataclass(frozen=True)
class ModuleProvider:
    """An explicit public inventory backed by an actual selected owned view.

    Only local/import roles supply provider assertions. Its contract's own
    requirements are not recursively resolved by another consumer's check.
    """

    contract: contracts.ModuleContract
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

    requirement: contracts.ModuleRequirement
    bindings: tuple[ModuleBinding, ...]
    providers: tuple[ModuleProvider, ...]
    obligations: tuple[ObligationResult, ...]
    property_contracts: tuple[
        tuple[contracts.ModulePropertyRequirement, PropertyContract], ...
    ]
    report: Report


@dataclass(frozen=True)
class ModuleWiring:
    """One-level interface evidence; never whole-provider equivalence or consistency."""

    consumer: contracts.ModuleContract
    requirements: tuple[ModuleRequirementResult, ...]
    report: Report
