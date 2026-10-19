"""Owned selected shape dependencies and raw finite behavior evidence."""

from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.documents.export import RDFExport
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report
from bijux_ontology.shacl.checking import ShaclResult

Triple = tuple[Node, Node, Node]
ShapeTerm = URIRef | BNode


@dataclass(frozen=True)
class ProfileCase:
    """An actual owned dataset and its explicitly expected selected conformance.

    A dataset must carry its own graph selection. Expected failures are raw
    behavioral evidence, not errors in the profile comparison itself.
    """

    name: str
    data: PreparedSource
    expected_conforms: bool

    def __post_init__(self) -> None:
        """Require finite case metadata and an actual prepared semantic input."""
        bound_text(self.name, Budget())
        if not self.name or not isinstance(self.data, PreparedSource):
            raise InputError("Profile cases require a name and PreparedSource")
        if type(self.expected_conforms) is not bool:
            raise InputError("Profile cases require explicit expected conformance")
        object.__setattr__(self, "name", str(self.name))


@dataclass(frozen=True)
class ShapeDependency:
    """A shape reference with exact original parameter and list assertions."""

    shape: ShapeTerm
    parameter: URIRef
    dependency: ShapeTerm
    via: tuple[Triple, ...]


@dataclass(frozen=True)
class ProfileComparison:
    """Raw maintained outcomes beside qualified selected-result agreement."""

    name: str
    source: ShaclResult | None
    derived: ShaclResult | None
    report: Report


@dataclass(frozen=True)
class ShapeProfile:
    """Finite structural closure, separate ontology and supplied-case evidence.

    Structural completion does not assert globally minimal extraction or
    universal behavioral preservation. Raw negative conformance is retained
    separately from comparison agreement.
    """

    source: PreparedSource | None
    ontology: PreparedSource | None
    roots: tuple[ShapeTerm, ...]
    dependencies: tuple[ShapeDependency, ...]
    structural_report: Report
    comparisons: tuple[ProfileComparison, ...]
    report: Report


@dataclass(frozen=True)
class ProfileExport:
    """Faithful role-separated portable RDF outputs and their qualification."""

    shapes: RDFExport | None
    ontology: RDFExport | None
    report: Report
