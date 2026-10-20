"""Named results for one typed class expression over a captured native theory."""

from dataclasses import dataclass

from bijux_ontology.reports import Report

from ...axioms.types import ClassExpression, NamedEntity
from ...backend.types import (
    BackendCapability,
    LoaderQualification,
    ProfileDocument,
    ReasonerIdentity,
)
from ...documents.types import OwlDocument


@dataclass(frozen=True)
class ClassQueryGroup:
    """One native equivalence node; flags and partial membership remain explicit."""

    members: tuple[NamedEntity, ...]
    top: bool
    bottom: bool
    complete: bool


@dataclass(frozen=True)
class ClassQueryResult:
    """Enumeration scope is distinct from expression satisfiability and consistency.

    A complete enumeration of an unsatisfiable expression remains a query result,
    never evidence that its instances are usable. No returned named class is
    labeled an asserted edge without a separate axiom inspection.
    """

    expression: ClassExpression | None
    direct: bool
    subclasses: tuple[ClassQueryGroup, ...]
    equivalents: ClassQueryGroup | None
    satisfiable: bool | None
    consistent: bool | None
    native_error: str | None
    theory_identity: str | None
    documents: tuple[OwlDocument | ProfileDocument, ...]
    loaders: tuple[LoaderQualification, ...]
    reasoner: ReasonerIdentity | None
    capability: BackendCapability | None
    consistency_report: Report
    report: Report
