"""Explicit role, annotation and module boundaries for native renaming."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget
from bijux_ontology.owl.modules.contracts import ModuleContract

from ...axioms.changes.types import AxiomBase
from ...axioms.types import EntityKind
from ...backend.types import BackendCapability
from ...documents.types import NativeProfile, OwlDocument
from ...theories.interfaces import ModuleInterfaceChange as ModuleInterfaceChange
from ...theories.interfaces import ModuleSet as ModuleSet

EntityRole: TypeAlias = EntityKind | Literal["all"]
AnnotationPosition = Literal["subject", "value", "domain", "range"]


@dataclass(frozen=True)
class RoleRename:
    """Rename selected structural roles; all is an explicit separate choice."""

    source: str
    target: str
    roles: tuple[EntityRole, ...]


@dataclass(frozen=True)
class AnnotationRole:
    """Interpret one actual annotation-property IRI position explicitly."""

    property: str
    position: AnnotationPosition
    action: Literal["preserve", "rename"]


@dataclass(frozen=True)
class RenamedModule:
    """Original raw precondition, checked native bytes and actual local edits."""

    original: OwlDocument
    source: OwlDocument
    base: AxiomBase
    additions: tuple[str, ...]
    removals: tuple[str, ...]
    profiles: tuple[NativeProfile, ...]


@dataclass(frozen=True)
class ModuleCheck:
    """A named actual logical or application check sharing the operation budget."""

    name: str
    check: Callable[[ModuleSet, Budget], Report]


@dataclass(frozen=True)
class RenameCandidate:
    """One complete reviewable transaction; no multifile publication is implied.

    Native change strings are bounded review renderings, not an executable OWL
    model. Only verified, complete candidates discharge the selected checks.
    """

    modules: tuple[RenamedModule, ...]
    interfaces: tuple[tuple[str, ModuleContract], ...]
    original_interfaces: tuple[tuple[str, ModuleContract], ...]
    checks: tuple[Report, ...]
    capability: BackendCapability | None
    report: Report
