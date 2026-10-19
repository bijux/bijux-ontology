"""Explicit owned module transactions and unchanged finite consumer obligations."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.reports import Report
from bijux_ontology.workflows.ontology.modules.types import (
    ModuleBinding,
    ModuleContract,
    ModuleWiring,
)

from ...axioms.changes.types import AxiomBase
from ...axioms.types import ProjectedAxiom
from ...backend.types import BackendCapability
from ...documents.types import OwlDocument
from ...inference.entailment.types import EntailmentCheck
from ...theories.interfaces import ModuleInterfaceChange, ModuleSet
from ...theories.types import TheoryCombination, TheoryObservation


@dataclass(frozen=True)
class AxiomMove:
    """Move exact annotated structures between explicitly owned effective documents."""

    source: str
    target: str
    axioms: tuple[ProjectedAxiom, ...]


@dataclass(frozen=True)
class ModuleInline:
    """Move all local axioms; original ontology headers remain separate and intact.

    Necessary source header declarations remain scaffolding, not a retained
    logical definition. No imported module or ontology header is merged.
    """

    source: str
    target: str


@dataclass(frozen=True)
class ModuleImportChange:
    """Only these exact caller-approved import declarations may change."""

    document: str
    additions: tuple[str, ...] = ()
    removals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModuleConsumer:
    """Keep the consumer's required contract fixed while explicitly rewiring it."""

    document: str
    before_bindings: tuple[ModuleBinding, ...]
    after_bindings: tuple[ModuleBinding, ...]
    entailments: tuple[ProjectedAxiom, ...]


@dataclass(frozen=True)
class NamespaceTransfer:
    """Caller-declared publication ownership, never inferred from IRI spelling.

    Presence in an original or required signature declaration does not itself
    establish authoritative namespace ownership. This records explicit policy.
    """

    namespace: str
    source: str
    target: str


@dataclass(frozen=True)
class ChangedModule:
    """One exact original base and its fully reread candidate, without publication."""

    original: OwlDocument
    source: OwlDocument
    base: AxiomBase
    additions: tuple[str, ...]
    removals: tuple[str, ...]


@dataclass(frozen=True)
class ConsumerModuleCheck:
    """Actual consumer closure proofs and the original explicit provider selection."""

    request: ModuleConsumer
    stage: Literal["before", "after"]
    entailments: EntailmentCheck
    wiring: ModuleWiring | None


@dataclass(frozen=True)
class ModuleRefactorCandidate:
    """A complete candidate set is withheld if any required stage fails.

    Checks retain paid raw negative or incomplete observations independently of
    candidate publication. Passing the finite interfaces is not full equivalence.
    """

    candidate: ModuleSet | None
    modules: tuple[ChangedModule, ...]
    original_interfaces: tuple[tuple[str, ModuleContract], ...]
    interfaces: tuple[tuple[str, ModuleContract], ...]
    namespace_transfers: tuple[NamespaceTransfer, ...]
    before: tuple[Report, ...]
    after: tuple[Report, ...]
    joint: tuple[TheoryObservation, ...]
    capability: BackendCapability | None
    consumer_checks: tuple[ConsumerModuleCheck, ...]
    deployment: TheoryCombination | None
    report: Report


__all__ = [
    "AxiomMove",
    "ChangedModule",
    "ConsumerModuleCheck",
    "ModuleConsumer",
    "ModuleImportChange",
    "ModuleInline",
    "ModuleInterfaceChange",
    "ModuleRefactorCandidate",
    "ModuleSet",
    "NamespaceTransfer",
]
